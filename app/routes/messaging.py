"""Messaging: WhatsApp & SMS settings, tests, broadcasts, receipts, retries, delivery-report webhooks and the provider's
shared account (platform admins)."""
from __future__ import annotations

import json
import secrets
from datetime import timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import automation, messaging
from ..crypto import encrypt
from ..db import Business, Customer, Notification, Sale, User, db_session, utcnow
from ..mpesa import mask_phone, normalize_phone
from ..security import audit, current_user, require_manager, require_owner, require_platform_admin
from .smart import base_url

router = APIRouter(prefix="/api")


def biz(db: Session, user: User) -> Business:
    return db.get(Business, user.business_id)


def hook_urls(request: Request, token: str) -> dict:
    base = base_url(request) + f"/api/messaging/hooks/{token}"
    return {"whatsapp_webhook": f"{base}/whatsapp", "whatsapp_verify_token": token, "sms_delivery_reports": f"{base}/sms-delivery"}


def ensure_hook_token(b: Business) -> str:
    if not b.msg_hook_token:
        b.msg_hook_token = secrets.token_urlsafe(24)
    return b.msg_hook_token


# ======================================================================= shop settings
class MessagingSettingsIn(BaseModel):
    source: Literal["platform", "own", "off"] = "platform"
    notify_channel: Literal["whatsapp", "sms"] = "whatsapp"
    sms_fallback: bool = True
    wa_token: Optional[str] = Field(default=None, max_length=600)
    wa_phone_id: str = Field(default="", max_length=40, pattern=r"^\d*$")
    wa_template: str = Field(default="", max_length=80, pattern=r"^[a-z0-9_]*$")
    wa_template_lang: str = Field(default="en", max_length=10)
    wa_app_secret: Optional[str] = Field(default=None, max_length=200)
    at_username: str = Field(default="", max_length=80)
    at_api_key: Optional[str] = Field(default=None, max_length=200)
    at_sender_id: str = Field(default="", max_length=11)


def settings_out(db: Session, b: Business, request: Request) -> dict:
    platform = messaging._creds_from(messaging.platform_get(db), "platform")
    return {"source": b.msg_source, "notify_channel": b.notify_channel, "sms_fallback": b.sms_fallback,
            "own": messaging.masked(messaging.own_values(b)), "effective": messaging.resolve(db, b).status(),
            "platform": {"available": (platform.whatsapp or platform.sms) and b.platform_messaging, "allowed": b.platform_messaging,
                         "whatsapp": platform.whatsapp, "sms": platform.sms},
            "hooks": hook_urls(request, b.msg_hook_token) if b.msg_hook_token else None, "owner_phone": b.owner_phone}


@router.get("/messaging/settings")
def get_settings(request: Request, user: User = Depends(require_owner), db: Session = Depends(db_session)):
    return settings_out(db, biz(db, user), request)


@router.put("/messaging/settings")
def put_settings(data: MessagingSettingsIn, request: Request, user: User = Depends(require_owner), db: Session = Depends(db_session)):
    b = biz(db, user)
    b.msg_source, b.notify_channel, b.sms_fallback = data.source, data.notify_channel, data.sms_fallback
    b.wa_phone_id, b.wa_template, b.wa_template_lang = data.wa_phone_id.strip(), data.wa_template.strip(), data.wa_template_lang.strip() or "en"
    b.at_username, b.at_sender_id = data.at_username.strip(), data.at_sender_id.strip()
    for field in ("wa_token", "wa_app_secret", "at_api_key"):
        v = getattr(data, field)
        if v and "•" not in v:
            setattr(b, field, encrypt(v.strip()))
    if data.source == "own":
        ensure_hook_token(b)
    audit(db, user, "messaging.settings", "business", b.id, f"source={data.source} channel={data.notify_channel}")
    db.commit()
    return settings_out(db, b, request)


@router.post("/messaging/clear-keys")
def clear_keys(which: Literal["whatsapp", "sms"], user: User = Depends(require_owner), db: Session = Depends(db_session)):
    b = biz(db, user)
    if which == "whatsapp":
        b.wa_token = b.wa_phone_id = b.wa_template = b.wa_app_secret = ""
    else:
        b.at_username = b.at_api_key = b.at_sender_id = ""
    audit(db, user, "messaging.clear", "business", b.id, which)
    db.commit()
    return {"ok": True}


@router.post("/messaging/test")
def test_connection(user: User = Depends(require_owner), db: Session = Depends(db_session)):
    c = messaging.resolve(db, biz(db, user))
    if not (c.whatsapp or c.sms):
        raise HTTPException(400, "No WhatsApp or SMS account is set up yet")
    return {"source": c.source, **messaging.check(c)}


class TestSendIn(BaseModel):
    phone: str = Field(default="", max_length=20)
    channel: Literal["whatsapp", "sms"] = "whatsapp"


@router.post("/messaging/test-send")
def test_send(data: TestSendIn, user: User = Depends(require_owner), db: Session = Depends(db_session)):
    b = biz(db, user)
    to = data.phone or b.owner_phone
    if not normalize_phone(to):
        raise HTTPException(400, "Enter the phone number to send the test to")
    body = f"✅ Grovia test message for {b.name}. If you can read this, {data.channel.upper()} messaging works."
    n = Notification(business_id=b.id, kind="test", channel=data.channel, recipient=normalize_phone(to), title="Test message", body=body,
                     status="sending")
    db.add(n)
    db.flush()
    try:
        n.status = automation.deliver(db, n)
        n.sent_at = utcnow()
    except messaging.MessagingError as e:
        n.status, n.error = "failed", str(e)[:240]
        db.commit()
        raise HTTPException(400, str(e))
    db.commit()
    return {"status": n.status, "channel": n.channel, "to": mask_phone(n.recipient), "note": n.error,
            "message": "Simulated — no keys set, nothing was sent" if n.status == "simulated" else f"Sent by {n.channel.upper()} to {mask_phone(n.recipient)}"}


# ======================================================================= broadcasts
AUDIENCES = {"all": "All customers with a phone number", "owing": "Customers who owe you money",
             "inactive": "Customers who haven't bought in 30+ days", "recent": "Customers who bought in the last 30 days"}


def audience_customers(db: Session, bid: int, audience: str) -> tuple[list[Customer], int]:
    customers = list(db.scalars(select(Customer).where(Customer.business_id == bid)))
    last = dict(db.execute(select(Sale.customer_id, func.max(Sale.created_at)).where(
        Sale.business_id == bid, Sale.status == "completed", Sale.customer_id.is_not(None)).group_by(Sale.customer_id)).all())
    owing = dict(db.execute(select(Sale.customer_id, func.sum(Sale.total - Sale.amount_paid)).where(
        Sale.business_id == bid, Sale.status == "completed", Sale.customer_id.is_not(None)).group_by(Sale.customer_id)).all())
    cutoff = utcnow() - timedelta(days=30)
    pick = {"all": lambda c: True, "owing": lambda c: (owing.get(c.id) or 0) > 0,
            "inactive": lambda c: c.id in last and last[c.id] < cutoff, "recent": lambda c: c.id in last and last[c.id] >= cutoff}[audience]
    chosen = [c for c in customers if normalize_phone(c.phone) and pick(c)]
    opted_out = sum(1 for c in chosen if c.marketing_opt_out)
    return [c for c in chosen if not c.marketing_opt_out], opted_out


@router.get("/messaging/audience")
def audience(audience: Literal["all", "owing", "inactive", "recent"] = "all", user: User = Depends(require_manager), db: Session = Depends(db_session)):
    people, opted_out = audience_customers(db, user.business_id, audience)
    return {"audience": audience, "label": AUDIENCES[audience], "count": len(people), "opted_out": opted_out,
            "sample": [c.name for c in people[:6]], "audiences": AUDIENCES}


class BroadcastIn(BaseModel):
    audience: Literal["all", "owing", "inactive", "recent"] = "all"
    message: str = Field(min_length=5, max_length=600)
    channel: Literal["whatsapp", "sms"] = "sms"


@router.post("/messaging/broadcast")
def broadcast(data: BroadcastIn, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    b = biz(db, user)
    people, opted_out = audience_customers(db, b.id, data.audience)
    if not people:
        raise HTTPException(400, "Nobody in that group has a phone number (or they all opted out)")
    batch = secrets.token_hex(4)
    for c in people:
        body = data.message.replace("{name}", c.name.split(" ")[0]) + f"\n— {b.name}"
        automation.enqueue(db, b, "broadcast", f"Broadcast to {AUDIENCES[data.audience].lower()}", body, recipient=c.phone,
                           channel=data.channel, dedupe_key=f"bc:{batch}:{c.id}", customer_id=c.id)
    audit(db, user, "messaging.broadcast", "business", b.id, f"{len(people)} × {data.channel}: {data.message[:120]}")
    db.commit()
    sent = automation.dispatch(db)
    return {"queued": len(people), "sent_now": sent, "opted_out": opted_out, "sms_parts": messaging.sms_parts(data.message),
            "effective": messaging.resolve(db, b).status()}


# ======================================================================= receipts, retries
class ReceiptSendIn(BaseModel):
    phone: str = Field(min_length=9, max_length=20)
    channel: Literal["whatsapp", "sms"] = "whatsapp"


@router.post("/sales/{sale_id}/send-receipt")
def send_receipt(sale_id: int, data: ReceiptSendIn, user: User = Depends(current_user), db: Session = Depends(db_session)):
    from .sales import sale_detail
    b = biz(db, user)
    sale = db.scalar(select(Sale).where(Sale.id == sale_id, Sale.business_id == b.id))
    if not sale:
        raise HTTPException(404, "Sale not found")
    if not normalize_phone(data.phone):
        raise HTTPException(400, "Enter a valid Safaricom/Airtel number")
    s = sale_detail(db, sale)
    lines = [f"{b.name} — receipt {s['reference']}", *(f"{i['quantity']} x {i['name']} {i['line_total']:,.0f}" for i in s["items"] if i["quantity"] > 0),
             f"TOTAL KSh {s['total']:,.0f} · Paid KSh {s['paid']:,.0f}" + (f" · Balance KSh {s['balance']:,.0f}" if s["balance"] > 0 else ""),
             b.receipt_footer or "Asante!"]
    n = Notification(business_id=b.id, kind="receipt", channel=data.channel, recipient=normalize_phone(data.phone),
                     title=f"Receipt {s['reference']}", body="\n".join(lines), status="sending", customer_id=sale.customer_id)
    db.add(n)
    db.flush()
    try:
        n.status = automation.deliver(db, n)
        n.sent_at = utcnow()
    except messaging.MessagingError as e:
        n.status, n.error = "failed", str(e)[:240]
        db.commit()
        raise HTTPException(400, str(e))
    db.commit()
    return {"status": n.status, "channel": n.channel, "to": mask_phone(n.recipient)}


@router.post("/notifications/{nid}/retry")
def retry(nid: int, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    n = db.scalar(select(Notification).where(Notification.id == nid, Notification.business_id == user.business_id))
    if not n or n.status not in ("failed", "simulated"):
        raise HTTPException(400, "Only failed or simulated messages can be retried")
    n.status, n.error = "queued", ""
    db.commit()
    automation.dispatch(db)
    db.refresh(n)
    return {"id": n.id, "status": n.status, "error": n.error, "channel": n.channel}


# ======================================================================= delivery-report webhooks (public, token-protected)
def hook_owner(db: Session, token: str) -> tuple[str, Business | None]:
    if len(token) < 20:
        raise HTTPException(404, "Not found")
    b = db.scalar(select(Business).where(Business.msg_hook_token == token))
    if b:
        return "own", b
    if secrets.compare_digest(token, messaging.platform_hook_token(db)):
        return "platform", None
    raise HTTPException(404, "Not found")


@router.get("/messaging/hooks/{token}/whatsapp", include_in_schema=False)
def wa_verify(token: str, request: Request, db: Session = Depends(db_session)):
    hook_owner(db, token)
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == token:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "Verification failed")


@router.post("/messaging/hooks/{token}/whatsapp", include_in_schema=False)
async def wa_hook(token: str, request: Request, db: Session = Depends(db_session)):
    kind, b = hook_owner(db, token)
    raw = await request.body()
    secret = messaging.own_values(b)["wa_app_secret"] if b else messaging.platform_get(db)["wa_app_secret"]
    if not messaging.whatsapp_signature_ok(secret, raw, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(403, "Bad signature")
    try:
        messaging.handle_whatsapp_hook(db, json.loads(raw or b"{}"), b.id if b else None)
        db.commit()
    except (ValueError, TypeError, AttributeError):
        db.rollback()
    return {"ok": True}


@router.post("/messaging/hooks/{token}/sms-delivery", include_in_schema=False)
async def at_hook(token: str, request: Request, db: Session = Depends(db_session)):
    kind, b = hook_owner(db, token)
    form = dict(await request.form())
    messaging.handle_at_report(db, form, b.id if b else None)
    db.commit()
    return {"ok": True}


# ======================================================================= provider's shared account (platform admins)
class PlatformMessagingIn(BaseModel):
    wa_token: Optional[str] = Field(default=None, max_length=600)
    wa_phone_id: str = Field(default="", max_length=40, pattern=r"^\d*$")
    wa_template: str = Field(default="", max_length=80, pattern=r"^[a-z0-9_]*$")
    wa_template_lang: str = Field(default="en", max_length=10)
    wa_app_secret: Optional[str] = Field(default=None, max_length=200)
    at_username: str = Field(default="", max_length=80)
    at_api_key: Optional[str] = Field(default=None, max_length=200)
    at_sender_id: str = Field(default="", max_length=11)


def usage_by_shop(db: Session) -> dict[int, dict]:
    since = utcnow() - timedelta(days=30)
    rows = db.execute(select(Notification.business_id, Notification.channel, func.count(Notification.id)).where(
        Notification.created_at >= since, Notification.channel.in_(("sms", "whatsapp")),
        Notification.status.in_(("sent", "delivered", "read"))).group_by(Notification.business_id, Notification.channel)).all()
    out: dict[int, dict] = {}
    for bid, ch, n in rows:
        out.setdefault(bid, {"sms": 0, "whatsapp": 0})[ch] = n
    return out


@router.get("/admin/messaging")
def admin_get(request: Request, admin: User = Depends(require_platform_admin), db: Session = Depends(db_session)):
    vals = messaging.platform_get(db)
    token = messaging.platform_hook_token(db)
    db.commit()
    c = messaging._creds_from(vals, "platform")
    usage = usage_by_shop(db)
    shops = [{"id": b.id, "name": b.name, "allowed": b.platform_messaging, "source": b.msg_source, **usage.get(b.id, {"sms": 0, "whatsapp": 0})}
             for b in db.scalars(select(Business).order_by(Business.name))]
    return {"values": messaging.masked(vals), "whatsapp": c.whatsapp, "sms": c.sms, "hooks": hook_urls(request, token), "shops": shops,
            "env_fallback": automation.provider_status()}


@router.put("/admin/messaging")
def admin_put(data: PlatformMessagingIn, request: Request, admin: User = Depends(require_platform_admin), db: Session = Depends(db_session)):
    messaging.platform_set(db, data.model_dump())
    audit(db, admin, "platform.messaging", "platform", None, "shared WhatsApp/SMS account updated")
    db.commit()
    return admin_get(request, admin, db)


@router.post("/admin/messaging/test")
def admin_test(admin: User = Depends(require_platform_admin), db: Session = Depends(db_session)):
    c = messaging._creds_from(messaging.platform_get(db), "platform")
    if not (c.whatsapp or c.sms):
        raise HTTPException(400, "Add the shared WhatsApp or Africa's Talking keys first")
    return messaging.check(c)


class AllowIn(BaseModel):
    allowed: bool


@router.post("/admin/businesses/{bid}/messaging")
def admin_allow(bid: int, data: AllowIn, admin: User = Depends(require_platform_admin), db: Session = Depends(db_session)):
    b = db.get(Business, bid)
    if not b:
        raise HTTPException(404, "Shop not found")
    b.platform_messaging = data.allowed
    from .admin import admin_audit
    admin_audit(db, admin, b.id, "platform.messaging_access", "shared messaging " + ("allowed" if data.allowed else "switched off"), b.id)
    db.commit()
    return {"id": b.id, "allowed": b.platform_messaging}
