"""Automation engine: Grovia works even when nobody opens the app.

Jobs (run every minute by an in-process scheduler, or by an external cron hitting /internal/cron):
  • Daily owner summary at the business's chosen hour (WhatsApp or SMS)
  • Debt reminders that escalate gently: 1st friendly, 2nd firmer, 3rd final, then stop
  • Low-stock / stock-out alert once a day
  • Fraud alerts for M-Pesa codes that M-Pesa never confirmed

Every message is written to the ``notifications`` outbox first with a ``dedupe_key`` (unique per business),
so jobs are idempotent and safe across restarts and multiple instances. A dispatcher then claims queued
rows atomically and delivers them through a provider:

  WhatsApp Cloud API   WHATSAPP_TOKEN + WHATSAPP_PHONE_NUMBER_ID
  Africa's Talking SMS AT_USERNAME + AT_API_KEY (+ optional AT_SENDER_ID)
  none configured      messages are marked "simulated" — visible in the app's outbox, nothing leaves the server

Note: WhatsApp only allows free-form business-initiated messages inside a 24h customer-service window;
outside it you must use pre-approved templates. Set WHATSAPP_TEMPLATE to use a template with one body variable.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import analytics
from .db import Business, Customer, Notification, Payment, Sale, SessionLocal, utcnow
from .mpesa import mask_phone, normalize_phone, stale_unverified_codes

log = logging.getLogger("duka.automation")


# ---------------------------------------------------------------- providers
def provider_status() -> dict:
    return {
        "whatsapp": bool(os.getenv("WHATSAPP_TOKEN") and os.getenv("WHATSAPP_PHONE_NUMBER_ID")),
        "sms": bool(os.getenv("AT_USERNAME") and os.getenv("AT_API_KEY")),
    }


def send_whatsapp(to: str, body: str) -> None:
    url = f"https://graph.facebook.com/v21.0/{os.environ['WHATSAPP_PHONE_NUMBER_ID']}/messages"
    template = os.getenv("WHATSAPP_TEMPLATE")
    payload = {"messaging_product": "whatsapp", "to": to}
    if template:
        payload |= {"type": "template", "template": {"name": template, "language": {"code": os.getenv("WHATSAPP_TEMPLATE_LANG", "en")},
                                                     "components": [{"type": "body", "parameters": [{"type": "text", "text": body[:1000]}]}]}}
    else:
        payload |= {"type": "text", "text": {"body": body[:4000]}}
    r = httpx.post(url, json=payload, headers={"Authorization": f"Bearer {os.environ['WHATSAPP_TOKEN']}"}, timeout=15)
    if r.status_code >= 300:
        raise RuntimeError(f"WhatsApp {r.status_code}: {r.text[:150]}")


def send_sms(to: str, body: str) -> None:
    username = os.environ["AT_USERNAME"]
    base = "https://api.sandbox.africastalking.com" if username == "sandbox" else "https://api.africastalking.com"
    data = {"username": username, "to": "+" + to, "message": body[:900]}
    if os.getenv("AT_SENDER_ID"):
        data["from"] = os.environ["AT_SENDER_ID"]
    r = httpx.post(f"{base}/version1/messaging", data=data, headers={"apiKey": os.environ["AT_API_KEY"], "Accept": "application/json"}, timeout=15)
    if r.status_code >= 300:
        raise RuntimeError(f"SMS {r.status_code}: {r.text[:150]}")


def deliver(n: Notification) -> str:
    """Returns final status. Raises on provider failure."""
    if n.channel == "inapp":
        return "sent"
    to = normalize_phone(n.recipient)
    if not to:
        raise RuntimeError("No valid phone number")
    avail = provider_status()
    channel = n.channel if avail.get(n.channel) else ("sms" if avail["sms"] else ("whatsapp" if avail["whatsapp"] else None))
    if channel is None:
        return "simulated"
    (send_whatsapp if channel == "whatsapp" else send_sms)(to, n.body)
    n.channel = channel
    return "sent"


def dispatch(db: Session, limit: int = 50) -> int:
    sent = 0
    ids = list(db.scalars(select(Notification.id).where(Notification.status == "queued").order_by(Notification.id).limit(limit)))
    for nid in ids:
        # atomic claim so two workers never send the same message
        if db.execute(update(Notification).where(Notification.id == nid, Notification.status == "queued").values(status="sending")).rowcount != 1:
            continue
        db.commit()
        n = db.get(Notification, nid)
        try:
            n.status = deliver(n)
            n.sent_at = utcnow()
            sent += 1
        except Exception as e:  # noqa: BLE001 — record any provider failure on the row
            n.status, n.error = "failed", str(e)[:240]
        db.commit()
    return sent


def enqueue(db: Session, b: Business, kind: str, title: str, body: str, *, recipient: str = "", channel: str | None = None,
            dedupe_key: str | None = None, customer_id: int | None = None) -> Notification | None:
    """Add to the outbox; returns None if this dedupe_key was already queued (job already done)."""
    ch = channel or ("inapp" if not recipient else b.notify_channel)
    n = Notification(business_id=b.id, kind=kind, title=title[:160], body=body, recipient=recipient, channel=ch,
                     dedupe_key=dedupe_key, customer_id=customer_id,
                     status="sent" if ch == "inapp" else "queued", sent_at=utcnow() if ch == "inapp" else None)
    try:
        with db.begin_nested():
            db.add(n)
            db.flush()
    except IntegrityError:
        return None
    return n


# ---------------------------------------------------------------- message composers
def compose_daily_summary(db: Session, b: Business, day=None) -> str:
    tz = analytics.tz_for(b)
    day = day or analytics.local_today(b)
    p = analytics.Period(day, day, "today")
    lo, hi = p.utc_bounds(tz)
    cur = analytics.sales_totals(db, b.id, lo, hi)
    plo, phi = analytics.Period(day - timedelta(days=7), day - timedelta(days=7), "").utc_bounds(tz)
    last_week = analytics.sales_totals(db, b.id, plo, phi)
    exp = analytics.expense_total(db, b.id, lo, hi)
    gross = cur["revenue"] - cur["cogs"]
    totals: dict[str, Decimal] = {}
    for m, a in db.execute(select(Payment.method, Payment.amount).join(Sale, Sale.id == Payment.sale_id).where(
            Payment.business_id == b.id, Sale.status == "completed", Payment.created_at >= lo, Payment.created_at < hi)):
        totals[m] = totals.get(m, Decimal(0)) + a
    ch = analytics.pct_change(cur["revenue"], last_week["revenue"])
    plan = analytics.restock_plan(db, b.id)
    urgent = [r for r in plan if r["stock_qty"] <= 0 or (r["days_left"] is not None and r["days_left"] <= 3)]
    owed = analytics.debtors(db, b.id)
    top = analytics.top_products(db, b.id, lo, hi, 1)
    lines = [f"*{b.name}* — {day:%a %d %b}", "",
             f"Mauzo: KSh {cur['revenue']:,.0f} ({cur['count']} sales)" + (f" · {'▲' if ch >= 0 else '▼'}{abs(ch):.0f}% vs last {day:%A}" if ch is not None else ""),
             f"Faida ghafi: KSh {gross:,.0f}" + (f" · Matumizi: KSh {exp:,.0f}" if exp else ""),
             "Pesa: " + (" · ".join(f"{k.upper() if k == 'mpesa' else k.title()} {v:,.0f}" for k, v in sorted(totals.items(), key=lambda x: -x[1])) or "—")]
    if top:
        lines.append(f"Top: {top[0]['name']} ({top[0]['quantity']})")
    if urgent:
        lines.append("⚠ Agiza: " + ", ".join(r["name"] for r in urgent[:4]))
    if owed:
        lines.append(f"Madeni: KSh {sum(d['balance'] for d in owed):,.0f} ({len(owed)} customers)")
    unverified = stale_unverified_codes(db, b, 15)
    if unverified:
        lines.append(f"🚩 {len(unverified)} M-Pesa code(s) typed at the till were never confirmed by M-Pesa")
    lines += ["", "— Grovia"]
    return "\n".join(lines)


REMINDER_STAGES = [
    "Habari {name}, ukumbusho wa kirafiki kutoka {shop}: salio lako ni KSh {bal:,.0f}. Lipa kwa M-Pesa {pay}. Asante! / Friendly reminder: your balance is KSh {bal:,.0f}.",
    "Habari {name}, salio lako {shop} la KSh {bal:,.0f} limechelewa siku {days}. Tafadhali lipa wiki hii kwa M-Pesa {pay}. / Your balance of KSh {bal:,.0f} is {days} days overdue — please pay this week.",
    "{name}, hii ni ukumbusho wa mwisho kutoka {shop}: KSh {bal:,.0f} (siku {days}). Tafadhali wasiliana nasi leo. / Final reminder: please settle KSh {bal:,.0f} or call us today.",
]


def pay_hint(b: Business, customer: Customer) -> str:
    if b.mpesa_mode != "off" and b.mpesa_shortcode:
        if b.mpesa_type == "paybill":
            return f"Paybill {b.mpesa_shortcode}, Account {normalize_phone(customer.phone) or customer.phone}"
        return f"Till {b.mpesa_shortcode}"
    return "au dukani"


# ---------------------------------------------------------------- jobs
def run_business_jobs(db: Session, b: Business, now_utc: datetime | None = None) -> dict:
    now_utc = now_utc or utcnow()
    tz = analytics.tz_for(b)
    local = analytics.to_local(now_utc, tz)
    today = local.date()
    done = {"summary": 0, "reminders": 0, "alerts": 0}

    # 1) Daily summary
    if b.daily_summary and b.owner_phone and local.hour >= b.summary_hour:
        if enqueue(db, b, "daily_summary", f"Daily summary {today:%d %b}", compose_daily_summary(db, b, today),
                   recipient=b.owner_phone, dedupe_key=f"summary:{today}"):
            done["summary"] = 1

    # 2) Escalating debt reminders — only in polite hours, at most one per customer per week, max 3
    if b.debt_reminders and 9 <= local.hour < 18:
        week = local.strftime("%G-W%V")
        for d in analytics.debtors(db, b.id):
            if d["oldest_days"] < b.reminder_after_days or d["balance"] < 50:
                continue
            c = db.get(Customer, d["customer_id"])
            if not normalize_phone(c.phone) or c.reminders_sent >= len(REMINDER_STAGES):
                continue
            if c.last_reminded_at and now_utc - c.last_reminded_at < timedelta(days=6):
                continue
            body = REMINDER_STAGES[c.reminders_sent].format(name=c.name.split()[0], shop=b.name, bal=d["balance"],
                                                            days=d["oldest_days"], pay=pay_hint(b, c))
            if enqueue(db, b, "debt_reminder", f"Reminder {c.reminders_sent + 1}/3 to {c.name}", body, recipient=c.phone,
                       dedupe_key=f"debt:{c.id}:{week}", customer_id=c.id):
                c.reminders_sent += 1
                c.last_reminded_at = now_utc
                done["reminders"] += 1

    # 3) Stock-out / imminent stock-out alert (in-app, once a day, from 7am)
    if local.hour >= 7:
        plan = [r for r in analytics.restock_plan(db, b.id) if r["stock_qty"] <= 0 or (r["days_left"] is not None and r["days_left"] <= 2)]
        if plan and enqueue(db, b, "low_stock", f"{len(plan)} product(s) need restocking today",
                            "\n".join(f"• {r['name']}: {r['stock_qty']} left → order {r['suggested_order']}" for r in plan[:10]),
                            dedupe_key=f"lowstock:{today}"):
            done["alerts"] += 1

    # 4) Fraud alert for unconfirmed M-Pesa codes (in-app + owner phone)
    for p in stale_unverified_codes(db, b, 15):
        sale = db.get(Sale, p.sale_id)
        body = (f"Code {p.external_ref} (KSh {p.amount:,.0f}) on sale {sale.reference} was typed at the till but M-Pesa never confirmed it. "
                f"Check your M-Pesa statement before the customer leaves for good.")
        if enqueue(db, b, "fraud_alert", f"Unconfirmed M-Pesa code {p.external_ref}", body, dedupe_key=f"fraud:{p.id}"):
            done["alerts"] += 1
            if b.owner_phone:
                enqueue(db, b, "fraud_alert", f"Unconfirmed M-Pesa code {p.external_ref}", f"🚩 {b.name}: {body}",
                        recipient=b.owner_phone, dedupe_key=f"fraud-sms:{p.id}")
    return done


def run_all(now_utc: datetime | None = None) -> dict:
    totals = {"businesses": 0, "summary": 0, "reminders": 0, "alerts": 0, "sent": 0}
    with SessionLocal() as db:
        for bid in list(db.scalars(select(Business.id))):
            b = db.get(Business, bid)
            try:
                r = run_business_jobs(db, b, now_utc)
                db.commit()
                totals["businesses"] += 1
                for k, v in r.items():
                    totals[k] += v
            except Exception:  # noqa: BLE001 — one bad business must not stop the others
                db.rollback()
                log.exception("automation failed for business %s", bid)
        totals["sent"] = dispatch(db)
    return totals


async def scheduler_loop(interval: int = 60) -> None:
    log.info("automation scheduler started (every %ss)", interval)
    while True:
        try:
            await asyncio.to_thread(run_all)
        except Exception:  # noqa: BLE001
            log.exception("automation run failed")
        await asyncio.sleep(interval)
