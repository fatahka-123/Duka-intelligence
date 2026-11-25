"""Authentication, business settings and staff management."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import AuditLog, Business, User, db_session, utcnow
from ..schemas import BusinessIn, LoginIn, PasswordChangeIn, RegisterIn, StaffIn, StaffUpdateIn, user_out
from ..security import (
    audit, clear_session_cookie, client_ip, current_user, dummy_verify, hash_password, login_throttle,
    ip_throttle, needs_rehash, require_owner, set_session_cookie, verify_password,
)

router = APIRouter(prefix="/api")


def business_out(b: Business) -> dict:
    return {"id": b.id, "name": b.name, "phone": b.phone, "currency": b.currency, "timezone": b.timezone,
            "address": b.address, "kra_pin": b.kra_pin, "receipt_footer": b.receipt_footer, "county": b.county,
            "mpesa_mode": b.mpesa_mode, "mpesa_type": b.mpesa_type, "mpesa_shortcode": b.mpesa_shortcode}


def session_payload(user: User, business: Business) -> dict:
    return {"user": user_out(user), "business": business_out(business)}


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
    if not user:
        dummy_verify()
    if not user or not verify_password(data.password, user.password_hash) or not user.active:
        login_throttle.fail(email_key)
        ip_throttle.fail(ip_key)
        raise HTTPException(401, "Email or password is incorrect")
    login_throttle.reset(email_key)
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
    user.password_hash = hash_password(data.new_password)
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
    staff = User(business_id=user.business_id, name=data.name, email=email, password_hash=hash_password(data.password), role=data.role)
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
    if data.password:
        staff.password_hash = hash_password(data.password)
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
