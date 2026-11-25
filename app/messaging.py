"""WhatsApp (Meta Cloud API) and SMS (Africa's Talking) for Grovia.

Where the keys come from, per shop (first match wins):
  own       – the shop's own keys, entered by the owner under Messaging (encrypted at rest)
  platform  – the provider's shared account, entered by a platform admin (encrypted, in platform_settings),
              used when the shop chose "platform" and the admin hasn't switched it off for that shop
  env       – server environment variables (WHATSAPP_TOKEN, AT_API_KEY, …) — the v0.3 behaviour, kept as a fallback
  none      – messages are "simulated": written to the outbox, nothing leaves the server

Delivery: the preferred channel is tried first; if WhatsApp fails and SMS fallback is on, the message goes by SMS.
Each sent message keeps the provider's id so delivery reports (WhatsApp statuses, Africa's Talking DLRs) can mark it
delivered / read / failed.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import secrets
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .crypto import decrypt, encrypt, mask
from .db import Business, Notification, PlatformSetting, utcnow
from .mpesa import normalize_phone

GRAPH = "https://graph.facebook.com/v21.0"
SECRET_FIELDS = {"wa_token", "wa_app_secret", "at_api_key"}
PLATFORM_FIELDS = ("wa_token", "wa_phone_id", "wa_template", "wa_template_lang", "wa_app_secret",
                   "at_username", "at_api_key", "at_sender_id", "hook_token")


class MessagingError(Exception):
    pass


@dataclass
class Creds:
    source: str = "none"  # own | platform | env | none
    wa: dict = field(default_factory=dict)  # token, phone_id, template, lang, app_secret
    at: dict = field(default_factory=dict)  # username, api_key, sender_id

    @property
    def whatsapp(self) -> bool:
        return bool(self.wa.get("token") and self.wa.get("phone_id"))

    @property
    def sms(self) -> bool:
        return bool(self.at.get("username") and self.at.get("api_key"))

    def status(self) -> dict:
        return {"source": self.source, "whatsapp": self.whatsapp, "sms": self.sms}


# ---------------------------------------------------------------- platform settings (shared account)
def platform_get(db: Session) -> dict:
    rows = {r.key: r.value for r in db.scalars(select(PlatformSetting))}
    return {k: (decrypt(rows.get(k, "")) if k in SECRET_FIELDS else rows.get(k, "")) for k in PLATFORM_FIELDS}


def platform_set(db: Session, values: dict) -> None:
    for k, v in values.items():
        if k not in PLATFORM_FIELDS or v is None:
            continue
        if k in SECRET_FIELDS and (not v or "•" in v):
            continue  # blank / masked = keep the stored secret
        row = db.get(PlatformSetting, k) or PlatformSetting(key=k)
        row.value = encrypt(v) if k in SECRET_FIELDS else str(v).strip()
        row.updated_at = utcnow()
        db.add(row)


def platform_hook_token(db: Session) -> str:
    row = db.get(PlatformSetting, "hook_token")
    if not row or not row.value:
        row = row or PlatformSetting(key="hook_token")
        row.value = secrets.token_urlsafe(24)
        db.add(row)
        db.flush()
    return row.value


def _creds_from(d: dict, source: str) -> Creds:
    return Creds(source=source,
                 wa={"token": d.get("wa_token", ""), "phone_id": d.get("wa_phone_id", ""), "template": d.get("wa_template", ""),
                     "lang": d.get("wa_template_lang") or "en", "app_secret": d.get("wa_app_secret", "")},
                 at={"username": d.get("at_username", ""), "api_key": d.get("at_api_key", ""), "sender_id": d.get("at_sender_id", "")})


def own_values(b: Business) -> dict:
    return {"wa_token": decrypt(b.wa_token), "wa_phone_id": b.wa_phone_id, "wa_template": b.wa_template,
            "wa_template_lang": b.wa_template_lang, "wa_app_secret": decrypt(b.wa_app_secret),
            "at_username": b.at_username, "at_api_key": decrypt(b.at_api_key), "at_sender_id": b.at_sender_id}


def env_values() -> dict:
    return {"wa_token": os.getenv("WHATSAPP_TOKEN", ""), "wa_phone_id": os.getenv("WHATSAPP_PHONE_NUMBER_ID", ""),
            "wa_template": os.getenv("WHATSAPP_TEMPLATE", ""), "wa_template_lang": os.getenv("WHATSAPP_TEMPLATE_LANG", "en"),
            "wa_app_secret": os.getenv("WHATSAPP_APP_SECRET", ""), "at_username": os.getenv("AT_USERNAME", ""),
            "at_api_key": os.getenv("AT_API_KEY", ""), "at_sender_id": os.getenv("AT_SENDER_ID", "")}


def resolve(db: Session, b: Business | None) -> Creds:
    """The keys this shop's messages will actually use."""
    if b is not None and b.msg_source == "off":
        return Creds()
    if b is not None and b.msg_source == "own":
        c = _creds_from(own_values(b), "own")
        if c.whatsapp or c.sms:
            return c
    if b is None or (b.msg_source in ("platform", "own") and b.platform_messaging):
        c = _creds_from(platform_get(db), "platform")
        if c.whatsapp or c.sms:
            return c
    c = _creds_from(env_values(), "env")
    return c if (c.whatsapp or c.sms) else Creds()


# ---------------------------------------------------------------- providers
def at_base(username: str) -> str:
    return "https://api.sandbox.africastalking.com" if username == "sandbox" else "https://api.africastalking.com"


def send_sms(c: Creds, to: str, body: str) -> str:
    data = {"username": c.at["username"], "to": "+" + to, "message": body[:900]}
    if c.at.get("sender_id"):
        data["from"] = c.at["sender_id"]
    try:
        r = httpx.post(f"{at_base(c.at['username'])}/version1/messaging", data=data, timeout=20,
                       headers={"apiKey": c.at["api_key"], "Accept": "application/json"})
    except httpx.HTTPError:
        raise MessagingError("Africa's Talking did not respond")
    if r.status_code >= 300:
        raise MessagingError(f"Africa's Talking {r.status_code}: {r.text[:140]}")
    try:
        rec = (r.json().get("SMSMessageData", {}).get("Recipients") or [{}])[0]
    except ValueError:
        raise MessagingError("Africa's Talking sent an unreadable reply")
    if int(rec.get("statusCode", 0) or 0) not in (100, 101, 102):
        raise MessagingError(f"SMS not accepted: {rec.get('status') or 'unknown error'}")
    return str(rec.get("messageId") or "")


def send_whatsapp(c: Creds, to: str, body: str) -> str:
    payload = {"messaging_product": "whatsapp", "to": to}
    if c.wa.get("template"):
        payload |= {"type": "template", "template": {"name": c.wa["template"], "language": {"code": c.wa.get("lang") or "en"},
                                                     "components": [{"type": "body", "parameters": [{"type": "text", "text": body[:1000]}]}]}}
    else:
        payload |= {"type": "text", "text": {"body": body[:4000]}}
    try:
        r = httpx.post(f"{GRAPH}/{c.wa['phone_id']}/messages", json=payload, timeout=20,
                       headers={"Authorization": f"Bearer {c.wa['token']}"})
    except httpx.HTTPError:
        raise MessagingError("WhatsApp did not respond")
    if r.status_code >= 300:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = r.text[:140]
        raise MessagingError(f"WhatsApp {r.status_code}: {msg[:160]}")
    return str((r.json().get("messages") or [{}])[0].get("id", ""))


def send(db: Session, b: Business | None, to: str, body: str, prefer: str = "whatsapp", creds: Creds | None = None) -> dict:
    """Send now. Returns {status, channel, ref, source, fallback}. Raises MessagingError when nothing could be sent."""
    phone = normalize_phone(to)
    if not phone:
        raise MessagingError("Not a valid Kenyan mobile number")
    c = creds or resolve(db, b)
    if not (c.whatsapp or c.sms):
        return {"status": "simulated", "channel": prefer, "ref": "", "source": "none", "fallback": False}
    order = ["whatsapp", "sms"] if prefer == "whatsapp" else ["sms", "whatsapp"]
    allow_fallback = b.sms_fallback if b is not None else True
    errors = []
    for i, ch in enumerate(order):
        if i > 0 and not allow_fallback:
            break
        if not (c.whatsapp if ch == "whatsapp" else c.sms):
            continue
        try:
            ref = (send_whatsapp if ch == "whatsapp" else send_sms)(c, phone, body)
            return {"status": "sent", "channel": ch, "ref": ref, "source": c.source, "fallback": bool(errors)}
        except MessagingError as e:
            errors.append(f"{ch}: {e}")
    raise MessagingError("; ".join(errors) or f"{prefer} is not set up")


def check(c: Creds) -> dict:
    """Verify keys without sending anything: AT returns the account balance, Meta returns the WhatsApp number."""
    out = {"whatsapp": None, "sms": None}
    if c.whatsapp:
        try:
            r = httpx.get(f"{GRAPH}/{c.wa['phone_id']}", params={"fields": "display_phone_number,verified_name,quality_rating"},
                          headers={"Authorization": f"Bearer {c.wa['token']}"}, timeout=15)
            if r.status_code == 200:
                d = r.json()
                out["whatsapp"] = {"ok": True, "detail": f"{d.get('verified_name') or 'WhatsApp'} · {d.get('display_phone_number', '')}"
                                   + (f" · quality {d['quality_rating']}" if d.get("quality_rating") else "")}
            else:
                out["whatsapp"] = {"ok": False, "detail": f"Meta rejected the token or phone number ID ({r.status_code})"}
        except httpx.HTTPError:
            out["whatsapp"] = {"ok": False, "detail": "Could not reach Meta"}
    if c.sms:
        try:
            r = httpx.get(f"{at_base(c.at['username'])}/version1/user", params={"username": c.at["username"]},
                          headers={"apiKey": c.at["api_key"], "Accept": "application/json"}, timeout=15)
            if r.status_code == 200:
                out["sms"] = {"ok": True, "detail": f"Balance {r.json().get('UserData', {}).get('balance', '?')}"
                              + (" (sandbox)" if c.at["username"] == "sandbox" else "")}
            else:
                out["sms"] = {"ok": False, "detail": f"Africa's Talking rejected the username or API key ({r.status_code})"}
        except httpx.HTTPError:
            out["sms"] = {"ok": False, "detail": "Could not reach Africa's Talking"}
    return out


def masked(values: dict) -> dict:
    return {k: (mask(v) if k in SECRET_FIELDS else v) for k, v in values.items() if k != "hook_token"}


# ---------------------------------------------------------------- delivery reports
STATUS_RANK = {"queued": 0, "sending": 1, "simulated": 2, "sent": 3, "delivered": 4, "read": 5}


def mark_delivery(db: Session, ref: str, status: str, error: str = "", business_id: int | None = None) -> bool:
    """A shop's own webhook only touches that shop's messages; the shared (platform) webhook only messages sent
    through the shared account."""
    if not ref or not status:
        return False
    stmt = select(Notification).where(Notification.provider_ref == ref)
    stmt = stmt.where(Notification.business_id == business_id) if business_id is not None else stmt.where(Notification.source == "platform")
    n = db.scalar(stmt.order_by(Notification.id.desc()).limit(1))
    if not n:
        return False
    if status == "failed":
        n.status, n.error = "failed", (error or "Delivery failed")[:240]
    elif STATUS_RANK.get(status, -1) > STATUS_RANK.get(n.status, -1):
        n.status = status
        if status in ("delivered", "read") and not n.delivered_at:
            n.delivered_at = utcnow()
    return True


def whatsapp_signature_ok(app_secret: str, body: bytes, header: str) -> bool:
    if not app_secret:
        return True  # not configured: rely on the secret URL token
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def handle_whatsapp_hook(db: Session, payload: dict, business_id: int | None = None) -> int:
    count = 0
    for entry in payload.get("entry", []) or []:
        for ch in entry.get("changes", []) or []:
            for st in (ch.get("value") or {}).get("statuses", []) or []:
                err = "; ".join(e.get("title", "") for e in st.get("errors", []) or [])
                count += mark_delivery(db, str(st.get("id", "")), {"sent": "sent", "delivered": "delivered", "read": "read",
                                                                    "failed": "failed"}.get(st.get("status"), ""), err, business_id)
    return count


def handle_at_report(db: Session, form: dict, business_id: int | None = None) -> bool:
    st = str(form.get("status", ""))
    mapped = "delivered" if st == "Success" else ("failed" if st in ("Failed", "Rejected", "AbsentSubscriber", "Expired") else "sent")
    return mark_delivery(db, str(form.get("id", "")), mapped, str(form.get("failureReason", "")), business_id)


# ---------------------------------------------------------------- helpers for the UI
def sms_parts(text: str) -> int:
    """SMS segments: 160 chars (GSM) or 70 (unicode, e.g. emoji); longer messages split into 153 / 67-char parts."""
    unicode = any(ord(ch) > 127 for ch in text)
    single, multi = (70, 67) if unicode else (160, 153)
    return 1 if len(text) <= single else math.ceil(len(text) / multi)
