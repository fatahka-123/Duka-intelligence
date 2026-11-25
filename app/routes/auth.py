"""Authentication, business settings and staff management."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

import logging
import secrets
from datetime import timedelta

from ..config import DEMO_STATE
from ..db import AuditLog, Business, User, db_session, utcnow
from ..schemas import BusinessIn, ForgotIn, LoginIn, ResetIn, PasswordChangeIn, RegisterIn, StaffIn, StaffUpdateIn, user_out
from ..security import (
    audit, clear_session_cookie, client_ip, current_user, dummy_verify, hash_password, login_throttle,
    PAUSED_MESSAGE, ip_throttle, is_platform_admin, needs_rehash, require_owner, set_session_cookie, verify_password,
)

router = APIRouter(prefix="/api")


def business_out(b: Business) -> dict:
    return {"id": b.id, "name": b.name, "phone": b.phone, "currency": b.currency, "timezone": b.timezone,
            "address": b.address, "kra_pin": b.kra_pin, "receipt_footer": b.receipt_footer, "county": b.county,
            "mpesa_mode": b.mpesa_mode, "mpesa_type": b.mpesa_type, "mpesa_shortcode": b.mpesa_shortcode}


def session_payload(user: User, business: Business) -> dict:
    return {"user": {**user_out(user), "platform_admin": is_platform_admin(user), "must_change_password": bool(user.must_change_password)},
            "business": business_out(business)}


@router.post("/auth/register")
def register(data: RegisterIn, response: Response, db: Session = Depends(db_session)):
    email = data.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "An account with this email already exists")
    business = Business(name=data.business_name, phone=data.phone)
    db.add(business)
    db.flush()
    user = User(business_id=business.id, name=data.name, email=email, password_hash=hash_password(data.password),
                role="owner", last_login_at=utcnow())
    db.add(user)
    db.flush()
    audit(db, user, "business.register", "business", business.id)
    db.commit()
    set_session_cookie(response, user)
    return session_payload(user, business)


@router.post("/auth/login")
def login(data: LoginIn, request: Request, response: Response, db: Session = Depends(db_session)):
    email = data.email.lower()
    ip_key, email_key = f"ip:{client_ip(request)}", f"email:{email}"
    wait = max(login_throttle.retry_after(email_key), ip_throttle.retry_after(ip_key))
    if wait:
        raise HTTPException(429, f"Too many failed sign-in attempts. Try again in {max(1, wait // 60)} minute(s).",
                            headers={"Retry-After": str(wait)})
    user = db.scalar(select(User).where(User.email == email))
    if not user and email.endswith("demo@duka.app"):
        if DEMO_STATE["state"] == "seeding":
            raise HTTPException(503, "The demo shop is still being set up — this takes 1–3 minutes on Render's free plan. Please try again shortly.")
        if DEMO_STATE["state"] == "failed":
            raise HTTPException(503, "The demo shop could not be created. Check the Render logs, or create your own account below.")
    if not user:
        dummy_verify()
    if not user or not verify_password(data.password, user.password_hash) or not user.active:
        login_throttle.fail(email_key)
        ip_throttle.fail(ip_key)
        raise HTTPException(401, "Email or password is incorrect")
    login_throttle.reset(email_key)
    business = db.get(Business, user.business_id)
    if not business.active and not is_platform_admin(user):
        raise HTTPException(403, PAUSED_MESSAGE)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(data.password)
    user.last_login_at = utcnow()
    db.commit()
    set_session_cookie(response, user)
    return session_payload(user, db.get(Business, user.business_id))


@router.post("/auth/logout")
def logout(response: Response):
    clear_session_cookie(response)
    return {"ok": True}


@router.post("/auth/logout-all")
def logout_all(response: Response, user: User = Depends(current_user), db: Session = Depends(db_session)):
    """Invalidate every session for this user (e.g. a lost phone)."""
    user.token_version = (user.token_version or 0) + 1
    audit(db, user, "auth.logout_all", "user", user.id)
    db.commit()
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/auth/me")
def me(user: User = Depends(current_user), db: Session = Depends(db_session)):
    return session_payload(user, db.get(Business, user.business_id))


@router.post("/auth/password")
def change_password(data: PasswordChangeIn, response: Response, user: User = Depends(current_user), db: Session = Depends(db_session)):
    if not verify_password(data.current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    if verify_password(data.new_password, user.password_hash):
        raise HTTPException(400, "Choose a new password that is different from the current one")
    user.password_hash = hash_password(data.new_password)
    user.must_change_password = False
    user.token_version = (user.token_version or 0) + 1  # sign out other devices
    audit(db, user, "auth.password_change", "user", user.id)
    db.commit()
    set_session_cookie(response, user)
    return {"ok": True}


# ---------------------------------------------------------------- business settings
@router.get("/business")
def get_business(user: User = Depends(current_user), db: Session = Depends(db_session)):
    return business_out(db.get(Business, user.business_id))


@router.put("/business")
def update_business(data: BusinessIn, user: User = Depends(require_owner), db: Session = Depends(db_session)):
    b = db.get(Business, user.business_id)
    for k, v in data.model_dump().items():
        setattr(b, k, v)
    audit(db, user, "business.update", "business", b.id)
    db.commit()
    return business_out(b)


# ---------------------------------------------------------------- staff
@router.get("/staff")
def list_staff(user: User = Depends(require_owner), db: Session = Depends(db_session)):
    return [user_out(u) for u in db.scalars(select(User).where(User.business_id == user.business_id).order_by(User.created_at))]


@router.post("/staff")
def add_staff(data: StaffIn, user: User = Depends(require_owner), db: Session = Depends(db_session)):
    email = data.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "An account with this email already exists")
    staff = User(business_id=user.business_id, name=data.name, email=email, password_hash=hash_password(data.password), role=data.role,
                 phone=data.phone, must_change_password=True)
    db.add(staff)
    db.flush()
    audit(db, user, "staff.add", "user", staff.id, f"role={data.role}")
    db.commit()
    return user_out(staff)


@router.patch("/staff/{uid}")
def update_staff(uid: int, data: StaffUpdateIn, user: User = Depends(require_owner), db: Session = Depends(db_session)):
    staff = db.scalar(select(User).where(User.id == uid, User.business_id == user.business_id))
    if not staff:
        raise HTTPException(404, "Staff member not found")
    if staff.id == user.id:
        raise HTTPException(400, "You cannot change your own owner account here")
    changes = []
    if data.role is not None:
        staff.role = data.role
        changes.append(f"role={data.role}")
    if data.active is not None:
        staff.active = data.active
        changes.append(f"active={data.active}")
    if data.phone is not None:
        staff.phone = data.phone
        changes.append("phone")
    if data.password:
        staff.password_hash = hash_password(data.password)
        staff.must_change_password = True
        changes.append("password reset")
    staff.token_version = (staff.token_version or 0) + 1  # force re-login with new permissions
    audit(db, user, "staff.update", "user", staff.id, ", ".join(changes))
    db.commit()
    return user_out(staff)


@router.get("/audit")
def audit_log(limit: int = 100, user: User = Depends(require_owner), db: Session = Depends(db_session)):
    names = {u.id: u.name for u in db.scalars(select(User).where(User.business_id == user.business_id))}
    rows = db.scalars(select(AuditLog).where(AuditLog.business_id == user.business_id)
                      .order_by(AuditLog.created_at.desc()).limit(min(max(limit, 1), 500)))
    return [{"id": a.id, "user": names.get(a.user_id, "—"), "action": a.action, "entity": a.entity,
             "entity_id": a.entity_id, "detail": a.detail, "created_at": a.created_at.isoformat()} for a in rows]


# ---------------------------------------------------------------- forgot password (SMS / WhatsApp code)
RESET_MINUTES = 15


def reset_phone(db: Session, user: User) -> str:
    """Where a reset code may go: the user's own phone, or for owners the shop's owner/business phone."""
    from ..mpesa import normalize_phone
    candidates = [user.phone]
    if user.role == "owner":
        b = db.get(Business, user.business_id)
        candidates += [b.owner_phone, b.phone]
    return next((p for p in map(normalize_phone, candidates) if p), "")


@router.post("/auth/forgot")
def forgot_password(data: ForgotIn, request: Request, db: Session = Depends(db_session)):
    from .. import automation
    key = f"ip:{client_ip(request)}"
    wait = ip_throttle.retry_after(key)
    if wait:
        raise HTTPException(429, f"Too many attempts. Try again in {max(1, wait // 60)} minute(s).")
    ip_throttle.fail(key)  # every request counts, so codes can't be sprayed
    generic = {"ok": True, "message": "If that account has a phone number on file, a 6-digit code is on its way by SMS/WhatsApp. "
                                      "Staff can also ask the shop owner to set a new password under Staff."}
    user = db.scalar(select(User).where(User.email == data.email.lower().strip(), User.active.is_(True)))
    providers = automation.provider_status()
    if not (providers["sms"] or providers["whatsapp"]):
        generic["message"] = ("Password reset by SMS isn't switched on for this server yet. Staff: ask the shop owner to set a new "
                              "password under Staff. Owners: ask whoever runs the server to run  python -m app.reset_password")
        return generic
    phone = reset_phone(db, user) if user else ""
    if not phone:
        return generic
    code = f"{secrets.randbelow(1_000_000):06d}"
    user.reset_code_hash, user.reset_expires_at, user.reset_attempts = hash_password(code), utcnow() + timedelta(minutes=RESET_MINUTES), 0
    db.commit()
    body = f"Duka Intelligence: your password reset code is {code}. It expires in {RESET_MINUTES} minutes. If you didn't ask for this, ignore it."
    try:
        (automation.send_sms if providers["sms"] else automation.send_whatsapp)(phone, body)
    except Exception:  # noqa: BLE001 — never reveal delivery details to an anonymous caller
        logging.getLogger("duka").exception("password reset delivery failed")
    return generic


@router.post("/auth/reset")
def reset_password(data: ResetIn, response: Response, db: Session = Depends(db_session)):
    user = db.scalar(select(User).where(User.email == data.email.lower().strip(), User.active.is_(True)))
    bad = HTTPException(400, "That code is wrong or has expired. Request a new one.")
    if not user or not user.reset_code_hash or not user.reset_expires_at or user.reset_expires_at < utcnow():
        dummy_verify()
        raise bad
    if user.reset_attempts >= 5:
        user.reset_code_hash = ""
        db.commit()
        raise bad
    if not verify_password(data.code, user.reset_code_hash):
        user.reset_attempts += 1
        db.commit()
        raise bad
    user.password_hash = hash_password(data.new_password)
    user.reset_code_hash, user.reset_expires_at, user.reset_attempts = "", None, 0
    user.token_version = (user.token_version or 0) + 1  # sign out every other device
    audit(db, user, "auth.password_reset", "user", user.id)
    db.commit()
    return {"ok": True, "message": "Password changed — sign in with your new password"}
