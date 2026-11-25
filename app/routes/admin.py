"""Platform admin: the provider's view across every client shop.

Only emails listed in PLATFORM_ADMINS can reach these endpoints (everyone else gets 404). Admins manage accounts —
create a client shop and its owner login, pause/resume a shop, reset a login — and see usage figures. They do not
browse a client's individual sales or customers from here.
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import AuditLog, Business, Product, Sale, User, db_session, utcnow
from ..schemas import f
from ..security import hash_password, is_platform_admin, require_platform_admin

router = APIRouter(prefix="/api/admin")


def temp_password() -> str:
    # easy to read out over the phone: no 0/O/1/l
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def admin_audit(db: Session, admin: User, business_id: int, action: str, detail: str, entity_id: int | None = None) -> None:
    db.add(AuditLog(business_id=business_id, user_id=admin.id, action=action, entity="platform", entity_id=entity_id,
                    detail=f"by provider ({admin.email}): {detail}"[:2000]))


def business_row(db: Session, b: Business, since) -> dict:
    owner = db.scalar(select(User).where(User.business_id == b.id, User.role == "owner").order_by(User.id))
    users = list(db.scalars(select(User).where(User.business_id == b.id)))
    sales_n, revenue = db.execute(select(func.count(Sale.id), func.coalesce(func.sum(Sale.total), 0)).where(
        Sale.business_id == b.id, Sale.status == "completed", Sale.created_at >= since)).one()
    last_sale = db.scalar(select(func.max(Sale.created_at)).where(Sale.business_id == b.id))
    last_login = max((u.last_login_at for u in users if u.last_login_at), default=None)
    last = max(filter(None, [last_sale, last_login]), default=None)
    return {
        "id": b.id, "name": b.name, "phone": b.phone, "county": b.county, "active": b.active, "note": b.admin_note,
        "created_at": b.created_at.isoformat(),
        "owner": {"id": owner.id, "name": owner.name, "email": owner.email, "must_change_password": owner.must_change_password,
                  "last_login_at": owner.last_login_at.isoformat() if owner.last_login_at else None} if owner else None,
        "staff_count": len(users), "products": db.scalar(select(func.count(Product.id)).where(Product.business_id == b.id, Product.active.is_(True))),
        "sales_30d": int(sales_n), "revenue_30d": f(Decimal(revenue)), "mpesa_mode": b.mpesa_mode,
        "last_activity": last.isoformat() if last else None,
    }


@router.get("/me")
def admin_me(admin: User = Depends(require_platform_admin)):
    return {"ok": True, "email": admin.email}


@router.get("/businesses")
def list_businesses(admin: User = Depends(require_platform_admin), db: Session = Depends(db_session)):
    since = utcnow() - timedelta(days=30)
    rows = [business_row(db, b, since) for b in db.scalars(select(Business).order_by(Business.created_at.desc()))]
    week = utcnow() - timedelta(days=7)
    return {
        "summary": {"shops": len(rows), "active": sum(r["active"] for r in rows),
                    "used_this_week": sum(1 for r in rows if r["last_activity"] and r["last_activity"] >= week.isoformat()),
                    "sales_30d": sum(r["sales_30d"] for r in rows), "revenue_30d": round(sum(r["revenue_30d"] for r in rows), 2)},
        "businesses": rows,
    }


class ClientIn(BaseModel):
    business_name: str = Field(min_length=2, max_length=140)
    owner_name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=190, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str = Field(default="", max_length=40)
    county: str = Field(default="", max_length=60)
    note: str = Field(default="", max_length=240)


@router.post("/businesses")
def create_client(data: ClientIn, admin: User = Depends(require_platform_admin), db: Session = Depends(db_session)):
    """Create a client shop with its owner login. The temporary password is shown once and must be changed at first sign-in."""
    email = data.email.lower().strip()
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "An account with this email already exists")
    b = Business(name=data.business_name.strip(), phone=data.phone, owner_phone=data.phone, county=data.county, admin_note=data.note)
    db.add(b)
    db.flush()
    pw = temp_password()
    owner = User(business_id=b.id, name=data.owner_name.strip(), email=email, phone=data.phone, password_hash=hash_password(pw),
                 role="owner", must_change_password=True)
    db.add(owner)
    db.flush()
    admin_audit(db, admin, b.id, "platform.create", f"shop created for {email}", b.id)
    db.commit()
    return {"business": business_row(db, b, utcnow() - timedelta(days=30)), "login": {"email": email, "temporary_password": pw}}


class StatusIn(BaseModel):
    active: bool
    note: str = Field(default="", max_length=240)


@router.post("/businesses/{bid}/status")
def set_status(bid: int, data: StatusIn, admin: User = Depends(require_platform_admin), db: Session = Depends(db_session)):
    b = db.get(Business, bid)
    if not b:
        raise HTTPException(404, "Shop not found")
    if not data.active and admin.business_id == b.id:
        raise HTTPException(400, "You can't pause your own shop")
    b.active = data.active
    if data.note:
        b.admin_note = data.note
    admin_audit(db, admin, b.id, "platform.resume" if data.active else "platform.pause", data.note or ("resumed" if data.active else "paused"), b.id)
    db.commit()
    return business_row(db, b, utcnow() - timedelta(days=30))


@router.get("/businesses/{bid}/users")
def business_users(bid: int, admin: User = Depends(require_platform_admin), db: Session = Depends(db_session)):
    if not db.get(Business, bid):
        raise HTTPException(404, "Shop not found")
    return [{"id": u.id, "name": u.name, "email": u.email, "role": u.role, "active": u.active, "phone": u.phone,
             "must_change_password": u.must_change_password, "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None}
            for u in db.scalars(select(User).where(User.business_id == bid).order_by(User.id))]


@router.post("/users/{uid}/reset-password")
def admin_reset_password(uid: int, admin: User = Depends(require_platform_admin), db: Session = Depends(db_session)):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "User not found")
    if u.id == admin.id:
        raise HTTPException(400, "Change your own password under Settings")
    pw = temp_password()
    u.password_hash, u.must_change_password, u.active = hash_password(pw), True, True
    u.token_version = (u.token_version or 0) + 1
    u.reset_code_hash, u.reset_attempts = "", 0
    admin_audit(db, admin, u.business_id, "platform.password_reset", f"temporary password issued for {u.email}", u.id)
    db.commit()
    return {"email": u.email, "temporary_password": pw}


# ---------------------------------------------------------------- contract ended: backup, then permanent deletion
@router.get("/businesses/{bid}/backup.json")
def admin_backup(bid: int, admin: User = Depends(require_platform_admin), db: Session = Depends(db_session)):
    """Download one client's full records (e.g. to hand over to them when the contract ends)."""
    from .business import backup_response, build_backup
    b = db.get(Business, bid)
    if not b:
        raise HTTPException(404, "Shop not found")
    data = build_backup(db, bid)
    admin_audit(db, admin, bid, "platform.backup", "full backup downloaded", bid)
    db.commit()
    return backup_response(data, b.name)


class DeleteIn(BaseModel):
    confirm_name: str = Field(max_length=140)
    reason: str = Field(min_length=3, max_length=240)


@router.post("/businesses/{bid}/delete")
def delete_business(bid: int, data: DeleteIn, admin: User = Depends(require_platform_admin), db: Session = Depends(db_session)):
    """Permanently erase a client shop and everything in it. Guard rails: the shop must already be paused, the admin must
    type the exact shop name, and the admin's own shop can never be deleted. A record of the deletion (no customer data)
    is kept in the admin's own activity log."""
    from sqlalchemy import delete as sql_delete
    from ..db import (Customer, Expense, MpesaTransaction, Notification, Payment, PurchaseOrder, PurchaseOrderItem, SaleItem,
                      Shift, StockMovement, Supplier)
    b = db.get(Business, bid)
    if not b:
        raise HTTPException(404, "Shop not found")
    if b.id == admin.business_id:
        raise HTTPException(400, "You can't delete your own shop")
    if any(is_platform_admin(u) for u in db.scalars(select(User).where(User.business_id == bid))):
        raise HTTPException(400, "This shop belongs to a platform admin and can't be deleted here")
    if b.active:
        raise HTTPException(400, "Pause the shop first, then delete it")
    if data.confirm_name.strip() != b.name.strip():
        raise HTTPException(400, "The shop name you typed doesn't match")
    counts = {
        "sales": db.scalar(select(func.count(Sale.id)).where(Sale.business_id == bid)),
        "products": db.scalar(select(func.count(Product.id)).where(Product.business_id == bid)),
        "logins": db.scalar(select(func.count(User.id)).where(User.business_id == bid)),
    }
    owner = db.scalar(select(User).where(User.business_id == bid, User.role == "owner").order_by(User.id))
    sale_ids = select(Sale.id).where(Sale.business_id == bid)
    order_ids = select(PurchaseOrder.id).where(PurchaseOrder.business_id == bid)
    # children before parents so foreign keys are never violated
    for stmt in (
        sql_delete(PurchaseOrderItem).where(PurchaseOrderItem.order_id.in_(order_ids)),
        sql_delete(PurchaseOrder).where(PurchaseOrder.business_id == bid),
        sql_delete(Notification).where(Notification.business_id == bid),
        sql_delete(Shift).where(Shift.business_id == bid),
        sql_delete(MpesaTransaction).where(MpesaTransaction.business_id == bid),
        sql_delete(AuditLog).where(AuditLog.business_id == bid),
        sql_delete(StockMovement).where(StockMovement.business_id == bid),
        sql_delete(Expense).where(Expense.business_id == bid),
        sql_delete(Payment).where(Payment.business_id == bid),
        sql_delete(SaleItem).where(SaleItem.sale_id.in_(sale_ids)),
        sql_delete(Sale).where(Sale.business_id == bid),
        sql_delete(Product).where(Product.business_id == bid),
        sql_delete(Customer).where(Customer.business_id == bid),
        sql_delete(Supplier).where(Supplier.business_id == bid),
        sql_delete(User).where(User.business_id == bid),
        sql_delete(Business).where(Business.id == bid),
    ):
        db.execute(stmt.execution_options(synchronize_session=False))
    db.add(AuditLog(business_id=admin.business_id, user_id=admin.id, action="platform.delete_shop", entity="platform", entity_id=bid,
                    detail=(f"Deleted client shop '{b.name}' (#{bid}, owner {owner.email if owner else '—'}): {counts['sales']} sales, "
                            f"{counts['products']} products, {counts['logins']} logins. Reason: {data.reason}")[:2000]))
    db.commit()
    return {"ok": True, "deleted": {"id": bid, "name": b.name, **counts}}
