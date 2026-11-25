"""v0.3 'smart' features: M-Pesa auto-reconciliation, shifts & cash-up, automation, Duka Score, loss watch,
market benchmarks and quick entry."""
from __future__ import annotations

import html
import secrets
from datetime import timedelta
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import analytics, automation, intelligence, mpesa
from ..config import CRON_SECRET, MPESA_ENFORCE_IPS, PUBLIC_BASE_URL
from ..crypto import encrypt, mask
from ..db import Business, Customer, MpesaTransaction, Notification, Payment, Sale, Shift, User, db_session, utcnow
from ..schemas import Clean, ExpenseIn, PaymentIn, SaleIn, SaleLineIn, StockReceiveIn, f, money
from ..security import audit, client_ip, current_user, require_manager, require_owner
from . import business as business_routes
from . import inventory as inventory_routes
from . import sales as sales_routes

router = APIRouter()


def biz(db: Session, user: User) -> Business:
    return db.get(Business, user.business_id)


def base_url(request: Request) -> str:
    return PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


def tx_out(t: MpesaTransaction) -> dict:
    return {"id": t.id, "source": t.source, "trans_id": t.trans_id, "amount": f(t.amount), "phone": mpesa.mask_phone(t.phone),
            "payer_name": t.payer_name, "bill_ref": t.bill_ref, "status": t.status, "sale_id": t.sale_id, "customer_id": t.customer_id,
            "matched_by": t.matched_by, "result_desc": t.result_desc, "created_at": t.created_at.isoformat()}


# ======================================================================= M-Pesa
class MpesaSettingsIn(Clean):
    mode: Literal["off", "simulator", "sandbox", "production"] = "off"
    shortcode: str = Field(default="", max_length=20, pattern=r"^\d*$")
    type: Literal["till", "paybill"] = "till"
    store_number: str = Field(default="", max_length=20, pattern=r"^\d*$")
    consumer_key: Optional[str] = Field(default=None, max_length=200)  # None/"" = keep existing
    consumer_secret: Optional[str] = Field(default=None, max_length=200)
    passkey: Optional[str] = Field(default=None, max_length=200)


def mpesa_settings_out(b: Business, request: Request) -> dict:
    from ..crypto import decrypt
    token = b.mpesa_callback_token
    base = base_url(request) + f"/api/mpesa/callback/{token}" if token else None
    return {"mode": b.mpesa_mode, "shortcode": b.mpesa_shortcode, "type": b.mpesa_type, "store_number": b.mpesa_store_number,
            "consumer_key": mask(decrypt(b.mpesa_consumer_key)), "consumer_secret": mask(decrypt(b.mpesa_consumer_secret)),
            "passkey": mask(decrypt(b.mpesa_passkey)),
            "callbacks": {"stk": f"{base}/stk", "confirmation": f"{base}/c2b/confirmation", "validation": f"{base}/c2b/validation"} if base else None}


@router.get("/api/mpesa/settings")
def get_mpesa_settings(request: Request, user: User = Depends(require_owner), db: Session = Depends(db_session)):
    return mpesa_settings_out(biz(db, user), request)


@router.put("/api/mpesa/settings")
def put_mpesa_settings(data: MpesaSettingsIn, request: Request, user: User = Depends(require_owner), db: Session = Depends(db_session)):
    b = biz(db, user)
    b.mpesa_mode, b.mpesa_shortcode, b.mpesa_type, b.mpesa_store_number = data.mode, data.shortcode, data.type, data.store_number
    for field in ("consumer_key", "consumer_secret", "passkey"):
        v = getattr(data, field)
        if v and "•" not in v:
            setattr(b, f"mpesa_{field}", encrypt(v))
    if data.mode != "off":
        mpesa.ensure_callback_token(b)
    if data.mode in ("sandbox", "production") and not (b.mpesa_shortcode and b.mpesa_consumer_key and b.mpesa_consumer_secret):
        raise HTTPException(400, "Sandbox/production need a shortcode, consumer key and consumer secret")
    audit(db, user, "mpesa.settings", "business", b.id, f"mode={data.mode}")
    db.commit()
    return mpesa_settings_out(b, request)


@router.post("/api/mpesa/register-urls")
def register_urls(request: Request, user: User = Depends(require_owner), db: Session = Depends(db_session)):
    b = biz(db, user)
    try:
        r = mpesa.Daraja(b, base_url(request)).register_c2b_urls()
    except mpesa.MpesaError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return r


class StkIn(BaseModel):
    phone: str = Field(min_length=9, max_length=20)
    amount: Decimal = Field(gt=0, le=Decimal("250000"))
    sale_id: Optional[int] = None  # set when collecting an existing balance


@router.post("/api/mpesa/stk")
def stk_push(data: StkIn, request: Request, user: User = Depends(current_user), db: Session = Depends(db_session)):
    b = biz(db, user)
    if not mpesa.mpesa_active(b):
        raise HTTPException(400, "Turn on M-Pesa in Settings first")
    phone = mpesa.normalize_phone(data.phone)
    if not phone:
        raise HTTPException(400, "Enter a valid Safaricom number, e.g. 0712 345 678")
    if data.sale_id and not db.scalar(select(Sale.id).where(Sale.id == data.sale_id, Sale.business_id == b.id, Sale.status == "completed")):
        raise HTTPException(404, "Sale not found")
    amount = int(Decimal(data.amount).to_integral_value(rounding="ROUND_CEILING"))  # M-Pesa only takes whole shillings
    tx = MpesaTransaction(business_id=b.id, source="stk", amount=Decimal(amount), phone=phone, sale_id=data.sale_id, status="pending")
    if b.mpesa_mode == "simulator":
        tx.checkout_request_id = "ws_SIM_" + secrets.token_hex(8)
    else:
        try:
            r = mpesa.Daraja(b, base_url(request)).stk_push(phone, amount, account_ref=b.name[:12] or "Duka", description="Purchase")
        except mpesa.MpesaError as e:
            raise HTTPException(400, str(e))
        tx.checkout_request_id = r["CheckoutRequestID"]
    db.add(tx)
    db.commit()
    return {**tx_out(tx), "message": f"Prompt sent to {mpesa.mask_phone(phone)} — ask the customer to enter their M-Pesa PIN"}


@router.get("/api/mpesa/stk/{tx_id}")
def stk_status(tx_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    b = biz(db, user)
    tx = db.scalar(select(MpesaTransaction).where(MpesaTransaction.id == tx_id, MpesaTransaction.business_id == b.id))
    if not tx:
        raise HTTPException(404, "Not found")
    # Simulator: the 'customer' enters their PIN ~4 seconds after the prompt
    if b.mpesa_mode == "simulator" and tx.status == "pending" and utcnow() - tx.created_at > timedelta(seconds=4):
        mpesa.simulate_stk_result(db, b, tx, success=not tx.phone.endswith("000"))
        db.commit()
    return tx_out(tx)


@router.get("/api/mpesa/feed")
def mpesa_feed(status: Optional[str] = None, hours: int = 48, user: User = Depends(current_user), db: Session = Depends(db_session)):
    """Recent confirmed M-Pesa money. The till polls this to show payments as they arrive."""
    since = utcnow() - timedelta(hours=min(max(hours, 1), 24 * 31))
    stmt = select(MpesaTransaction).where(MpesaTransaction.business_id == user.business_id, MpesaTransaction.created_at >= since,
                                          MpesaTransaction.status != "pending")
    if status:
        stmt = stmt.where(MpesaTransaction.status == status)
    rows = list(db.scalars(stmt.order_by(MpesaTransaction.created_at.desc()).limit(100)))
    unmatched = [t for t in rows if t.status == "unmatched"]
    return {"items": [tx_out(t) for t in rows], "unmatched_count": len(unmatched), "unmatched_total": f(sum((t.amount for t in unmatched), Decimal(0)))}


class AttachIn(BaseModel):
    sale_id: Optional[int] = None
    customer_id: Optional[int] = None


@router.post("/api/mpesa/{tx_id}/attach")
def attach_tx(tx_id: int, data: AttachIn, user: User = Depends(current_user), db: Session = Depends(db_session)):
    """Link an unmatched M-Pesa payment to an existing sale balance or to a customer's debts."""
    b = biz(db, user)
    tx = db.scalar(select(MpesaTransaction).where(MpesaTransaction.id == tx_id, MpesaTransaction.business_id == b.id).with_for_update())
    if not tx or tx.status != "unmatched":
        raise HTTPException(400, "This payment is not available to attach")
    if data.sale_id:
        sale = db.scalar(select(Sale).where(Sale.id == data.sale_id, Sale.business_id == b.id, Sale.status == "completed").with_for_update())
        if not sale or sale.total <= sale.amount_paid:
            raise HTTPException(400, "That sale has no balance to pay")
        mpesa.apply_to_sale(db, sale, tx, tx.amount, "cashier", user.id)
        tx.sale_id, tx.customer_id = sale.id, sale.customer_id
    elif data.customer_id:
        c = db.scalar(select(Customer).where(Customer.id == data.customer_id, Customer.business_id == b.id))
        if not c or mpesa.apply_to_customer_debts(db, c, tx, "cashier") <= 0:
            raise HTTPException(400, "That customer has no balance to pay")
        tx.customer_id = c.id
    else:
        raise HTTPException(400, "Choose a sale or customer")
    tx.status, tx.matched_by = "matched", "cashier"
    audit(db, user, "mpesa.attach", "mpesa", tx.id, f"{tx.trans_id} {f(tx.amount)}")
    db.commit()
    return tx_out(tx)


@router.post("/api/mpesa/{tx_id}/ignore")
def ignore_tx(tx_id: int, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    tx = db.scalar(select(MpesaTransaction).where(MpesaTransaction.id == tx_id, MpesaTransaction.business_id == user.business_id))
    if not tx or tx.status != "unmatched":
        raise HTTPException(400, "Only unmatched payments can be dismissed")
    tx.status = "ignored"
    audit(db, user, "mpesa.ignore", "mpesa", tx.id, f"{tx.trans_id} {f(tx.amount)}")
    db.commit()
    return tx_out(tx)


@router.get("/api/mpesa/check-code")
def check_code(code: str, amount: Decimal = Decimal(0), user: User = Depends(current_user), db: Session = Depends(db_session)):
    return mpesa.check_manual_code(db, biz(db, user), code, amount)


class SimulateIn(BaseModel):
    amount: Decimal = Field(gt=0, le=Decimal("250000"))
    phone: str = Field(default="", max_length=20)
    name: str = Field(default="", max_length=60)
    bill_ref: str = Field(default="", max_length=40)


@router.post("/api/mpesa/simulate")
def simulate(data: SimulateIn, user: User = Depends(current_user), db: Session = Depends(db_session)):
    """Simulator mode only: pretend a customer just paid to the till/paybill."""
    b = biz(db, user)
    if b.mpesa_mode != "simulator":
        raise HTTPException(400, "Simulation is only available in simulator mode")
    tx = mpesa.simulate_c2b(db, b, money(data.amount), data.phone, data.name, data.bill_ref)
    db.commit()
    return tx_out(tx)


# ---------------------------------------------------------------- Daraja callbacks (public, token-protected)
def callback_business(db: Session, token: str, request: Request) -> Business:
    if MPESA_ENFORCE_IPS and client_ip(request) not in mpesa.SAFARICOM_IPS:
        raise HTTPException(403, "Forbidden")
    b = db.scalar(select(Business).where(Business.mpesa_callback_token == token)) if len(token) >= 20 else None
    if not b or b.mpesa_mode not in ("sandbox", "production"):
        raise HTTPException(404, "Not found")
    return b


@router.post("/api/mpesa/callback/{token}/stk", include_in_schema=False)
async def stk_callback(token: str, request: Request, db: Session = Depends(db_session)):
    b = callback_business(db, token, request)
    try:
        mpesa.handle_stk_callback(db, b, await request.json())
        db.commit()
    except (KeyError, ValueError, TypeError):
        db.rollback()
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


@router.post("/api/mpesa/callback/{token}/c2b/validation", include_in_schema=False)
async def c2b_validation(token: str, request: Request, db: Session = Depends(db_session)):
    callback_business(db, token, request)
    return {"ResultCode": "0", "ResultDesc": "Accepted"}


@router.post("/api/mpesa/callback/{token}/c2b/confirmation", include_in_schema=False)
async def c2b_confirmation(token: str, request: Request, db: Session = Depends(db_session)):
    b = callback_business(db, token, request)
    try:
        mpesa.handle_c2b_confirmation(db, b, await request.json())
        db.commit()
    except (KeyError, ValueError, TypeError):
        db.rollback()
    return {"ResultCode": 0, "ResultDesc": "Success"}


# ======================================================================= Shifts & cash-up
def shift_live(db: Session, sh: Shift) -> dict:
    end = sh.closed_at or utcnow()
    by_method = dict(db.execute(select(Payment.method, func.coalesce(func.sum(Payment.amount), 0)).join(Sale, Sale.id == Payment.sale_id).where(
        Payment.business_id == sh.business_id, Payment.user_id == sh.user_id, Sale.status == "completed",
        Payment.created_at >= sh.opened_at, Payment.created_at <= end).group_by(Payment.method)).all())
    count = db.scalar(select(func.count(Sale.id)).where(Sale.business_id == sh.business_id, Sale.user_id == sh.user_id,
                                                        Sale.status == "completed", Sale.created_at >= sh.opened_at, Sale.created_at <= end))
    cash = Decimal(by_method.get("cash", 0))
    expected = sh.opening_float + cash - (sh.cash_removed or 0)
    user = db.get(User, sh.user_id)
    return {"id": sh.id, "user": user.name if user else "—", "user_id": sh.user_id, "status": sh.status,
            "opened_at": sh.opened_at.isoformat(), "closed_at": sh.closed_at.isoformat() if sh.closed_at else None,
            "opening_float": f(sh.opening_float), "cash_sales": f(cash), "mpesa": f(by_method.get("mpesa", 0)),
            "other": f(sum((Decimal(v) for k, v in by_method.items() if k not in ("cash", "mpesa")), Decimal(0))),
            "cash_removed": f(sh.cash_removed), "expected_cash": f(sh.expected_cash if sh.status == "closed" else expected),
            "counted_cash": f(sh.counted_cash) if sh.counted_cash is not None else None,
            "variance": f(sh.variance) if sh.variance is not None else None, "sales_count": count, "notes": sh.notes}


class ShiftOpenIn(BaseModel):
    opening_float: Decimal = Field(default=Decimal(0), ge=0, le=Decimal("10000000"))


class ShiftCloseIn(Clean):
    counted_cash: Decimal = Field(ge=0, le=Decimal("100000000"))
    cash_removed: Decimal = Field(default=Decimal(0), ge=0)
    notes: str = Field(default="", max_length=240)


def open_shift_for(db: Session, user: User) -> Shift | None:
    return db.scalar(select(Shift).where(Shift.business_id == user.business_id, Shift.user_id == user.id, Shift.status == "open"))


@router.get("/api/shifts/current")
def current_shift(user: User = Depends(current_user), db: Session = Depends(db_session)):
    sh = open_shift_for(db, user)
    return shift_live(db, sh) if sh else None


@router.post("/api/shifts/open")
def open_shift(data: ShiftOpenIn, user: User = Depends(current_user), db: Session = Depends(db_session)):
    if open_shift_for(db, user):
        raise HTTPException(400, "You already have an open shift")
    sh = Shift(business_id=user.business_id, user_id=user.id, opening_float=money(data.opening_float))
    db.add(sh)
    db.commit()
    return shift_live(db, sh)


@router.post("/api/shifts/close")
def close_shift(data: ShiftCloseIn, user: User = Depends(current_user), db: Session = Depends(db_session)):
    sh = open_shift_for(db, user)
    if not sh:
        raise HTTPException(400, "No open shift")
    sh.cash_removed = money(data.cash_removed)
    live = shift_live(db, sh)
    sh.expected_cash = money(live["expected_cash"])
    sh.counted_cash = money(data.counted_cash)
    sh.variance = sh.counted_cash - sh.expected_cash
    sh.mpesa_total = money(live["mpesa"])
    sh.sales_count = live["sales_count"]
    sh.closed_at, sh.status, sh.notes = utcnow(), "closed", data.notes
    b = biz(db, user)
    if abs(sh.variance) >= 100:
        word = "SHORT" if sh.variance < 0 else "OVER"
        body = (f"{user.name} closed the till {word} by KSh {abs(sh.variance):,.0f}. Expected KSh {sh.expected_cash:,.0f}, "
                f"counted KSh {sh.counted_cash:,.0f}." + (f" Note: {data.notes}" if data.notes else ""))
        automation.enqueue(db, b, "cash_variance", f"Till {word.lower()} KSh {abs(sh.variance):,.0f} — {user.name}", body, dedupe_key=f"shift:{sh.id}")
        if b.owner_phone:
            automation.enqueue(db, b, "cash_variance", f"Till {word.lower()}", f"⚠ {b.name}: {body}", recipient=b.owner_phone,
                               dedupe_key=f"shift-sms:{sh.id}")
    audit(db, user, "shift.close", "shift", sh.id, f"variance {f(sh.variance):.2f}")
    db.commit()
    return shift_live(db, sh)


@router.get("/api/shifts")
def list_shifts(user: User = Depends(require_manager), db: Session = Depends(db_session)):
    rows = db.scalars(select(Shift).where(Shift.business_id == user.business_id).order_by(Shift.opened_at.desc()).limit(60))
    return [shift_live(db, s) for s in rows]


# ======================================================================= Automation & notifications
class AutomationIn(Clean):
    owner_phone: str = Field(default="", max_length=40)
    notify_channel: Literal["whatsapp", "sms"] = "whatsapp"
    daily_summary: bool = True
    summary_hour: int = Field(default=20, ge=0, le=23)
    debt_reminders: bool = False
    reminder_after_days: int = Field(default=7, ge=1, le=90)
    price_alerts: bool = True
    benchmark_opt_in: bool = False
    county: str = Field(default="", max_length=60)


def automation_out(b: Business) -> dict:
    return {"owner_phone": b.owner_phone, "notify_channel": b.notify_channel, "daily_summary": b.daily_summary, "summary_hour": b.summary_hour,
            "debt_reminders": b.debt_reminders, "reminder_after_days": b.reminder_after_days, "price_alerts": b.price_alerts,
            "benchmark_opt_in": b.benchmark_opt_in, "county": b.county, "providers": automation.provider_status()}


@router.get("/api/automation/settings")
def get_automation(user: User = Depends(require_owner), db: Session = Depends(db_session)):
    return automation_out(biz(db, user))


@router.put("/api/automation/settings")
def put_automation(data: AutomationIn, user: User = Depends(require_owner), db: Session = Depends(db_session)):
    b = biz(db, user)
    if data.owner_phone and not mpesa.normalize_phone(data.owner_phone):
        raise HTTPException(400, "Owner phone must be a Kenyan mobile number")
    for k, v in data.model_dump().items():
        setattr(b, k, v)
    audit(db, user, "automation.settings", "business", b.id)
    db.commit()
    return automation_out(b)


@router.get("/api/automation/preview-summary")
def preview_summary(user: User = Depends(require_manager), db: Session = Depends(db_session)):
    return {"text": automation.compose_daily_summary(db, biz(db, user))}


@router.post("/api/automation/run")
def run_now(send_summary_now: bool = False, user: User = Depends(require_owner), db: Session = Depends(db_session)):
    """Run this business's automations immediately (handy for testing the setup)."""
    b = biz(db, user)
    result = automation.run_business_jobs(db, b)
    if send_summary_now:
        if not b.owner_phone:
            raise HTTPException(400, "Add the owner's phone number first")
        automation.enqueue(db, b, "daily_summary", "Summary (sent manually)", automation.compose_daily_summary(db, b),
                           recipient=b.owner_phone, dedupe_key=f"summary-manual:{utcnow():%Y%m%d%H%M%S}")
        result["summary"] = 1
    db.commit()
    result["sent"] = automation.dispatch(db)
    return result


@router.get("/api/notifications")
def notifications(kind: Optional[str] = None, outbox: bool = False, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    stmt = select(Notification).where(Notification.business_id == user.business_id)
    if kind:
        stmt = stmt.where(Notification.kind == kind)
    if outbox:
        stmt = stmt.where(Notification.channel != "inapp")
    else:
        stmt = stmt.where(Notification.channel == "inapp")
    rows = db.scalars(stmt.order_by(Notification.created_at.desc()).limit(100))
    return [{"id": n.id, "kind": n.kind, "channel": n.channel, "recipient": mpesa.mask_phone(n.recipient) if n.recipient else "",
             "title": n.title, "body": n.body, "status": n.status, "error": n.error, "created_at": n.created_at.isoformat(),
             "sent_at": n.sent_at.isoformat() if n.sent_at else None} for n in rows]


@router.get("/api/notifications/unread")
def unread(user: User = Depends(current_user), db: Session = Depends(db_session)):
    if user.role == "cashier":
        return {"count": 0}
    return {"count": db.scalar(select(func.count(Notification.id)).where(Notification.business_id == user.business_id,
                                                                         Notification.channel == "inapp", Notification.status == "sent"))}


@router.post("/api/notifications/read-all")
def read_all(user: User = Depends(require_manager), db: Session = Depends(db_session)):
    for n in db.scalars(select(Notification).where(Notification.business_id == user.business_id, Notification.channel == "inapp",
                                                   Notification.status == "sent")):
        n.status = "read"
    db.commit()
    return {"ok": True}


@router.post("/internal/cron", include_in_schema=False)
def cron(x_cron_secret: str = Header(default="")):
    if not CRON_SECRET or not secrets.compare_digest(x_cron_secret, CRON_SECRET):
        raise HTTPException(403, "Forbidden")
    return automation.run_all()


# ======================================================================= Duka Score & lender links
@router.get("/api/score")
def score(user: User = Depends(require_owner), db: Session = Depends(db_session)):
    return intelligence.duka_score(db, biz(db, user))


class ShareIn(BaseModel):
    days: int = Field(default=30, ge=1, le=90)


@router.post("/api/score/share")
def share(data: ShareIn, request: Request, user: User = Depends(require_owner), db: Session = Depends(db_session)):
    b = biz(db, user)
    token = intelligence.share_token(b, data.days)
    audit(db, user, "score.share", "business", b.id, f"{data.days} days")
    db.commit()
    return {"url": f"{base_url(request)}/verify/{token}", "expires_in_days": data.days, "verification_code": token.split(".")[1][:8].upper()}


@router.post("/api/score/revoke")
def revoke(user: User = Depends(require_owner), db: Session = Depends(db_session)):
    b = biz(db, user)
    b.share_version += 1
    audit(db, user, "score.revoke", "business", b.id)
    db.commit()
    return {"ok": True}


@router.get("/verify/{token}", response_class=HTMLResponse, include_in_schema=False)
def verify_page(token: str, db: Session = Depends(db_session)):
    found = intelligence.read_share_token(db, token)
    if not found:
        return HTMLResponse("<h2 style='font-family:sans-serif;padding:40px'>This link has expired or was revoked by the business owner.</h2>", status_code=404)
    b, data = found
    s = intelligence.duka_score(db, b)
    e = html.escape
    rows = "".join(f"<tr><td>{e(m['month'])}{' (partial)' if m['partial'] else ''}</td><td>{m['revenue']:,.0f}</td><td>{m['gross_profit']:,.0f}</td>"
                   f"<td>{m['expenses']:,.0f}</td><td>{m['net_profit']:,.0f}</td><td>{m['transactions']}</td></tr>" for m in s["months"])
    comps = "".join(f"<tr><td>{e(c['label'])}</td><td><b>{c['score']}</b>/100</td><td>{e(c['detail'])}</td></tr>" for c in s["components"].values())
    code = token.split(".")[1][:8].upper()
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Verified business statement · {e(b.name)}</title>
<style>body{{font-family:system-ui,sans-serif;background:#f5f8f5;color:#14251e;margin:0;padding:24px}}main{{max-width:820px;margin:auto;background:#fff;border-radius:16px;padding:28px;border:1px solid #e8eee9}}
h1{{margin:0 0 4px}}.muted{{color:#718078;font-size:13px}}.score{{display:flex;gap:18px;align-items:center;margin:22px 0;padding:18px;background:#10251d;color:#fff;border-radius:14px}}
.big{{font-size:48px;font-weight:800}}table{{width:100%;border-collapse:collapse;margin:14px 0;font-size:13px}}th,td{{text-align:left;padding:8px;border-bottom:1px solid #eef2ee}}th{{color:#718078;font-size:11px;text-transform:uppercase}}
.badge{{background:#d9f36b;color:#163523;border-radius:99px;padding:4px 10px;font-weight:700;font-size:12px}}</style></head><body><main>
<span class="badge">✓ Verified by Duka Intelligence</span><h1 style="margin-top:12px">{e(b.name)}</h1>
<div class="muted">{e(b.address or '')} {('· ' + e(b.county)) if b.county else ''} · Statement generated {utcnow():%d %b %Y %H:%M} UTC · Verification code <b>{code}</b></div>
<div class="score"><div class="big">{s['score']}</div><div><b>Duka Score · Grade {s['grade']} · {e(s['status'])}</b><br>
<span style="color:#b9cbbf">{s['history_days']} days of records · avg monthly revenue KSh {s['average_monthly_revenue']:,.0f} · avg monthly net profit KSh {s['average_monthly_net_profit']:,.0f}</span></div></div>
<h3>Monthly performance (KSh)</h3><table><tr><th>Month</th><th>Revenue</th><th>Gross profit</th><th>Expenses</th><th>Net profit</th><th>Sales</th></tr>{rows}</table>
<h3>Score breakdown</h3><table><tr><th>Factor</th><th>Score</th><th>Evidence</th></tr>{comps}</table>
<p class="muted">Figures come from transactions recorded in Duka by the business. M-Pesa receipts are confirmed by Safaricom only where the business has linked M-Pesa.
{e(s['disclaimer'])} Shared by the owner; this link expires automatically and can be revoked at any time.</p></main></body></html>"""
    return HTMLResponse(page, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


# ======================================================================= Loss watch & benchmarks
@router.get("/api/loss-watch")
def get_loss_watch(days: int = 30, user: User = Depends(require_owner), db: Session = Depends(db_session)):
    return intelligence.loss_watch(db, biz(db, user), min(max(days, 7), 180))


@router.get("/api/benchmarks")
def get_benchmarks(user: User = Depends(require_manager), db: Session = Depends(db_session)):
    b = biz(db, user)
    return {"opted_in": b.benchmark_opt_in, "items": intelligence.benchmarks(db, b)}


# ======================================================================= Quick entry
class QuickIn(BaseModel):
    text: str = Field(min_length=2, max_length=300)


@router.post("/api/quick-entry/parse")
def quick_parse(data: QuickIn, user: User = Depends(current_user), db: Session = Depends(db_session)):
    return intelligence.parse_quick_entry(db, biz(db, user), data.text, can_manage=user.role != "cashier")


class QuickLine(BaseModel):
    product_id: int
    quantity: int = Field(gt=0, le=100000)


class QuickAction(BaseModel):
    intent: Literal["sale", "receive", "expense", "payment"]
    lines: list[QuickLine] = []
    payment_method: str = "cash"
    customer_id: Optional[int] = None
    payment_ref: str = ""
    expense: Optional[dict] = None
    payment: Optional[dict] = None
    client_ref: Optional[str] = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")


@router.post("/api/quick-entry/execute")
def quick_execute(action: QuickAction, user: User = Depends(current_user), db: Session = Depends(db_session)):
    """Carry out a confirmed quick-entry action through the same code paths (and checks) as the normal screens."""
    if action.intent == "sale":
        sale = sales_routes.create_sale(SaleIn(items=[SaleLineIn(product_id=l.product_id, quantity=l.quantity) for l in action.lines],
                                               customer_id=action.customer_id, payment_method=action.payment_method,
                                               payment_ref=action.payment_ref, client_ref=action.client_ref), user, db)
        return {"done": "sale", "sale": sale, "message": f"Sale {sale['reference']} recorded · KSh {sale['total']:,.0f}"}
    if user.role == "cashier" and action.intent in ("receive", "expense"):
        raise HTTPException(403, "Your role does not have access to this action")
    if action.intent == "receive":
        for l in action.lines:
            inventory_routes.receive_stock(l.product_id, StockReceiveIn(quantity=l.quantity, note="Quick entry"), user, db)
        return {"done": "receive", "message": f"Stock received for {len(action.lines)} product(s)"}
    if action.intent == "expense" and action.expense:
        x = business_routes.create_expense(ExpenseIn(**action.expense), user, db)
        return {"done": "expense", "expense": x, "message": f"Expense recorded · {x['category']} KSh {x['amount']:,.0f}"}
    if action.intent == "payment" and action.payment:
        cid, remaining = int(action.payment["customer_id"]), money(action.payment["amount"])
        method = action.payment.get("method", "cash")
        open_sales = list(db.scalars(select(Sale).where(Sale.business_id == user.business_id, Sale.customer_id == cid,
                                                        Sale.status == "completed", Sale.total > Sale.amount_paid).order_by(Sale.created_at)))
        if not open_sales:
            raise HTTPException(400, "This customer has no outstanding balance")
        for s in open_sales:
            if remaining <= 0:
                break
            pay = min(remaining, s.total - s.amount_paid)
            sales_routes.record_payment(s.id, PaymentIn(amount=pay, payment_method=method), user, db)
            remaining -= pay
        return {"done": "payment", "message": f"Payment of KSh {money(action.payment['amount']) - remaining:,.0f} recorded"}
    raise HTTPException(400, "Nothing to do")
