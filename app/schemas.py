"""Request models and response serializers."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .config import ADJUSTMENT_REASONS, PAYMENT_METHODS
from .db import Customer, Expense, Product, Sale, Supplier, User

PaymentMethod = Literal[PAYMENT_METHODS]  # type: ignore[valid-type]
AdjustReason = Literal[ADJUSTMENT_REASONS]  # type: ignore[valid-type]


def money(v) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def f(v) -> float:
    return float(v or 0)


def _strip(v):
    return v.strip() if isinstance(v, str) else v


class Clean(BaseModel):
    """Strips whitespace from every string field except passwords."""

    @field_validator("*", mode="before")
    @classmethod
    def _s(cls, v, info):
        return v if "password" in (info.field_name or "") else _strip(v)


# ---------------------------------------------------------------- auth / account
class RegisterIn(Clean):
    business_name: str = Field(min_length=2, max_length=140)
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=190, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=10, max_length=128)
    phone: str = Field(default="", max_length=40)


class LoginIn(Clean):
    email: str = Field(max_length=190)
    password: str = Field(max_length=128)


class ForgotIn(BaseModel):
    email: str = Field(max_length=190)


class ResetIn(BaseModel):
    email: str = Field(max_length=190)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    new_password: str = Field(min_length=10, max_length=128)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


class BusinessIn(Clean):
    name: str = Field(min_length=2, max_length=140)
    phone: str = Field(default="", max_length=40)
    address: str = Field(default="", max_length=240)
    kra_pin: str = Field(default="", max_length=20)
    receipt_footer: str = Field(default="", max_length=240)
    timezone: str = Field(default="Africa/Nairobi", max_length=64)

    @field_validator("timezone")
    @classmethod
    def valid_tz(cls, v):
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError("Unknown timezone")
        return v


class StaffIn(Clean):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=190, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=10, max_length=128)
    role: Literal["manager", "cashier"] = "cashier"
    phone: str = Field(default="", max_length=40)


class StaffUpdateIn(BaseModel):
    role: Optional[Literal["manager", "cashier"]] = None
    active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=10, max_length=128)
    phone: Optional[str] = Field(default=None, max_length=40)


# ---------------------------------------------------------------- catalogue
class ProductIn(Clean):
    name: str = Field(min_length=1, max_length=160)
    sku: str = Field(default="", max_length=80)
    barcode: str = Field(default="", max_length=100)
    category: str = Field(default="General", max_length=100)
    unit: str = Field(default="pcs", max_length=20)
    cost_price: Decimal = Field(ge=0, le=Decimal("999999999"))
    selling_price: Decimal = Field(gt=0, le=Decimal("999999999"))
    stock_qty: int = Field(default=0, ge=0, le=10_000_000)  # only used on create (opening stock)
    reorder_level: int = Field(default=5, ge=0, le=10_000_000)

    @field_validator("category")
    @classmethod
    def default_category(cls, v):
        return v or "General"


class StockReceiveIn(Clean):
    quantity: int = Field(gt=0, le=10_000_000)
    unit_cost: Optional[Decimal] = Field(default=None, ge=0)
    supplier_id: Optional[int] = None
    update_cost_price: bool = True
    note: str = Field(default="Stock received", max_length=240)


class StockAdjustIn(Clean):
    counted_qty: Optional[int] = Field(default=None, ge=0, le=10_000_000)  # set absolute stock after a physical count
    change: Optional[int] = Field(default=None, ge=-10_000_000, le=10_000_000)  # or a relative change (e.g. -2 damaged)
    reason: AdjustReason = "count"
    note: str = Field(default="", max_length=240)


# ---------------------------------------------------------------- sales
class SaleLineIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0, le=100_000)


class SaleIn(Clean):
    items: list[SaleLineIn] = Field(min_length=1, max_length=200)
    customer_id: Optional[int] = None
    discount: Decimal = Field(default=Decimal("0"), ge=0)
    amount_paid: Optional[Decimal] = Field(default=None, ge=0)
    payment_method: PaymentMethod = "cash"
    payment_ref: str = Field(default="", max_length=60)
    client_ref: Optional[str] = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")


class PaymentIn(Clean):
    amount: Decimal = Field(gt=0)
    payment_method: PaymentMethod = "cash"
    payment_ref: str = Field(default="", max_length=60)


class VoidIn(Clean):
    reason: str = Field(min_length=3, max_length=240)


# ---------------------------------------------------------------- people / money
class CustomerIn(Clean):
    name: str = Field(min_length=1, max_length=160)
    phone: str = Field(default="", max_length=40)
    notes: str = Field(default="", max_length=240)
    credit_limit: Decimal = Field(default=Decimal("0"), ge=0)
    marketing_opt_out: bool = False


class SupplierIn(Clean):
    name: str = Field(min_length=1, max_length=160)
    phone: str = Field(default="", max_length=40)
    notes: str = Field(default="", max_length=240)


class ExpenseIn(Clean):
    category: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=240)
    amount: Decimal = Field(gt=0, le=Decimal("999999999"))


class AssistantIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------- serializers
def user_out(u: User) -> dict:
    return {"id": u.id, "name": u.name, "email": u.email, "role": u.role, "active": u.active, "phone": u.phone or "",
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None}


def product_out(p: Product, show_cost: bool = True) -> dict:
    out = {
        "id": p.id, "name": p.name, "sku": p.sku, "barcode": p.barcode, "category": p.category, "unit": p.unit,
        "selling_price": f(p.selling_price), "stock_qty": p.stock_qty, "reorder_level": p.reorder_level,
        "low_stock": p.stock_qty <= p.reorder_level,
    }
    if show_cost:
        out["cost_price"] = f(p.cost_price)
        out["margin_pct"] = round(f(p.selling_price - p.cost_price) / f(p.selling_price) * 100, 1) if p.selling_price else 0
    return out


def sale_out(s: Sale, customer_name: str | None = None, cashier: str | None = None) -> dict:
    balance = s.total - s.amount_paid
    return {
        "id": s.id, "reference": s.reference, "subtotal": f(s.subtotal), "discount": f(s.discount), "total": f(s.total),
        "paid": f(s.amount_paid), "balance": f(balance), "method": s.payment_method, "status": s.status,
        "payment_status": "voided" if s.status == "voided" else ("paid" if balance <= 0 else ("unpaid" if s.amount_paid == 0 else "part_paid")),
        "customer_id": s.customer_id, "customer_name": customer_name, "cashier": cashier,
        "void_reason": s.void_reason or None, "created_at": s.created_at.isoformat(),
    }


def customer_out(c: Customer, balance=0) -> dict:
    return {"id": c.id, "name": c.name, "phone": c.phone, "notes": c.notes, "credit_limit": f(c.credit_limit), "balance": f(balance),
            "marketing_opt_out": bool(c.marketing_opt_out)}


def supplier_out(s: Supplier) -> dict:
    return {"id": s.id, "name": s.name, "phone": s.phone, "notes": s.notes}


def expense_out(x: Expense) -> dict:
    return {"id": x.id, "category": x.category, "description": x.description, "amount": f(x.amount), "created_at": x.created_at.isoformat()}
