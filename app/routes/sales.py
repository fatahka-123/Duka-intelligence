"""Point of sale, receipts, debt payments and voids."""
from __future__ import annotations

import secrets
from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from .. import analytics, mpesa
from ..db import Business, Customer, MpesaTransaction, Payment, Product, Sale, SaleItem, StockMovement, User, db_session, utcnow
from ..schemas import PaymentIn, SaleIn, VoidIn, f, money, sale_out
from ..security import audit, current_user, require_manager

router = APIRouter(prefix="/api")


def _names(db: Session, bid: int) -> tuple[dict, dict]:
    users = {u.id: u.name for u in db.scalars(select(User).where(User.business_id == bid))}
    customers = dict(db.execute(select(Customer.id, Customer.name).where(Customer.business_id == bid)).all())
    return users, customers


def sale_detail(db: Session, sale: Sale) -> dict:
    users, customers = _names(db, sale.business_id)
    out = sale_out(sale, customers.get(sale.customer_id), users.get(sale.user_id))
    out["items"] = [{"product_id": i.product_id, "name": i.product_name, "quantity": i.quantity,
                     "unit_price": f(i.unit_price), "line_total": f(i.line_total)}
                    for i in db.scalars(select(SaleItem).where(SaleItem.sale_id == sale.id).order_by(SaleItem.id))]
    out["payments"] = [{"amount": f(p.amount), "method": p.method, "ref": p.external_ref, "by": users.get(p.user_id),
                        "created_at": p.created_at.isoformat()}
                       for p in db.scalars(select(Payment).where(Payment.sale_id == sale.id).order_by(Payment.created_at))]
    out["returns"] = [{"quantity": m.quantity, "note": m.note, "restocked": m.movement_type == "return", "created_at": m.created_at.isoformat()}
                      for m in db.scalars(select(StockMovement).where(StockMovement.business_id == sale.business_id,
                                                                      StockMovement.movement_type.in_(("return", "return_damaged")),
                                                                      StockMovement.note.like(f"Return {sale.reference}:%")))]
    return out


@router.get("/sales")
def list_sales(
    start: Optional[date] = None, end: Optional[date] = None, period: Optional[str] = None,
    status: Optional[str] = None, unpaid: bool = False, customer_id: Optional[int] = None, limit: int = 200,
    user: User = Depends(current_user), db: Session = Depends(db_session),
):
    stmt = select(Sale).where(Sale.business_id == user.business_id)
    if start or end or period:
        business = db.get(Business, user.business_id)
        p = analytics.resolve_period(business, period, start or end, end or start)
        lo, hi = p.utc_bounds(analytics.tz_for(business))
        stmt = stmt.where(Sale.created_at >= lo, Sale.created_at < hi)
    if status:
        stmt = stmt.where(Sale.status == status)
    if unpaid:
        stmt = stmt.where(Sale.status == "completed", Sale.total > Sale.amount_paid)
    if customer_id:
        stmt = stmt.where(Sale.customer_id == customer_id)
    if user.role == "cashier":  # cashiers only see their own sales history
        stmt = stmt.where(Sale.user_id == user.id)
    users, customers = _names(db, user.business_id)
    rows = db.scalars(stmt.order_by(Sale.created_at.desc()).limit(min(max(limit, 1), 1000)))
    return [sale_out(s, customers.get(s.customer_id), users.get(s.user_id)) for s in rows]


@router.get("/sales/{sale_id}")
def get_sale(sale_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    sale = db.scalar(select(Sale).where(Sale.id == sale_id, Sale.business_id == user.business_id))
    if not sale or (user.role == "cashier" and sale.user_id != user.id):
        raise HTTPException(404, "Sale not found")
    return sale_detail(db, sale)


@router.post("/sales")
def create_sale(data: SaleIn, user: User = Depends(current_user), db: Session = Depends(db_session)):
    bid = user.business_id
    # Idempotency: a retried request (flaky network, double tap) returns the original sale
    if data.client_ref:
        existing = db.scalar(select(Sale).where(Sale.business_id == bid, Sale.client_ref == data.client_ref))
        if existing:
            return {**sale_detail(db, existing), "duplicate": True}

    # Merge duplicate lines so stock checks see the real total per product
    wanted: dict[int, int] = {}
    for line in data.items:
        wanted[line.product_id] = wanted.get(line.product_id, 0) + line.quantity

    products: dict[int, Product] = {}
    for pid in sorted(wanted):  # consistent lock order avoids deadlocks between tills
        p = db.scalar(select(Product).where(Product.id == pid, Product.business_id == bid, Product.active.is_(True)).with_for_update())
        if not p:
            raise HTTPException(404, f"Product {pid} not found")
        if p.stock_qty < wanted[pid]:
            raise HTTPException(400, f"Insufficient stock for {p.name}: {p.stock_qty} available, {wanted[pid]} requested")
        products[pid] = p

    customer = None
    if data.customer_id:
        customer = db.scalar(select(Customer).where(Customer.id == data.customer_id, Customer.business_id == bid))
        if not customer:
            raise HTTPException(404, "Customer not found")

    subtotal = money(sum((products[pid].selling_price * q for pid, q in wanted.items()), Decimal("0")))
    discount = money(data.discount)
    if discount > subtotal:
        raise HTTPException(400, "Discount cannot exceed subtotal")
    total = subtotal - discount
    if data.amount_paid is not None:
        paid = money(data.amount_paid)
    else:
        paid = Decimal("0.00") if data.payment_method == "credit" else total
    if paid > total:
        raise HTTPException(400, "Amount paid cannot exceed the sale total — record change given, not the cash tendered")
    balance = total - paid
    if balance > 0:
        if not customer:
            raise HTTPException(400, "Select a customer for credit or part-paid sales so the balance can be followed up")
        if customer.credit_limit > 0:
            owed = Decimal(db.scalar(select(func.coalesce(func.sum(Sale.total - Sale.amount_paid), 0)).where(
                Sale.business_id == bid, Sale.customer_id == customer.id, Sale.status == "completed")))
            if owed + balance > customer.credit_limit:
                raise HTTPException(400, f"{customer.name}'s credit limit is KSh {f(customer.credit_limit):,.2f}; "
                                         f"they already owe KSh {f(owed):,.2f}")

    # Fraud guard: an M-Pesa code can only ever be used once, and must be real when M-Pesa is linked
    verified_tx = None
    if data.payment_method == "mpesa" and paid > 0 and data.payment_ref:
        check = mpesa.check_manual_code(db, db.get(Business, bid), data.payment_ref, paid)
        if check["status"] in ("reused", "invalid", "short"):
            raise HTTPException(400, check["message"])
        if check["status"] == "verified":
            verified_tx = db.get(MpesaTransaction, check["tx_id"])

    sale = Sale(business_id=bid, customer_id=data.customer_id, user_id=user.id, client_ref=data.client_ref,
                reference="DK-" + secrets.token_hex(4).upper(), subtotal=subtotal, discount=discount, total=total,
                amount_paid=paid, payment_method=data.payment_method, status="completed")
    db.add(sale)
    try:
        db.flush()
    except IntegrityError:  # concurrent retry with the same client_ref won the race
        db.rollback()
        existing = db.scalar(select(Sale).where(Sale.business_id == bid, Sale.client_ref == data.client_ref))
        if existing:
            return {**sale_detail(db, existing), "duplicate": True}
        raise
    for pid, qty in wanted.items():
        p = products[pid]
        p.stock_qty -= qty
        db.add(SaleItem(sale_id=sale.id, product_id=p.id, product_name=p.name, quantity=qty, unit_price=p.selling_price,
                        unit_cost=p.cost_price, line_total=money(p.selling_price * qty)))
        db.add(StockMovement(business_id=bid, product_id=p.id, user_id=user.id, movement_type="sale", quantity=-qty,
                             unit_cost=p.cost_price, balance_after=p.stock_qty, note=sale.reference))
    if paid > 0:
        method = "cash" if data.payment_method == "credit" else data.payment_method
        db.add(Payment(business_id=bid, sale_id=sale.id, user_id=user.id, amount=paid, method=method, external_ref=data.payment_ref.upper()))
    if verified_tx:
        verified_tx.status, verified_tx.sale_id, verified_tx.customer_id, verified_tx.matched_by = "matched", sale.id, data.customer_id, verified_tx.matched_by or "cashier"
    db.commit()
    out = sale_detail(db, sale)
    out["mpesa_verified"] = bool(verified_tx)
    return out


@router.post("/sales/{sale_id}/payment")
def record_payment(sale_id: int, data: PaymentIn, user: User = Depends(current_user), db: Session = Depends(db_session)):
    sale = db.scalar(select(Sale).where(Sale.id == sale_id, Sale.business_id == user.business_id).with_for_update())
    if not sale:
        raise HTTPException(404, "Sale not found")
    if sale.status != "completed":
        raise HTTPException(400, "Cannot take payment on a voided sale")
    if data.payment_method == "credit":
        raise HTTPException(400, "Choose how the money was received")
    amount = money(data.amount)
    if data.payment_method == "mpesa" and data.payment_ref:
        check = mpesa.check_manual_code(db, db.get(Business, user.business_id), data.payment_ref, amount)
        if check["status"] in ("reused", "invalid", "short"):
            raise HTTPException(400, check["message"])
        if check["status"] == "verified":
            tx = db.get(MpesaTransaction, check["tx_id"])
            tx.status, tx.sale_id, tx.customer_id, tx.matched_by = "matched", sale.id, sale.customer_id, "cashier"
    if sale.amount_paid + amount > sale.total:
        raise HTTPException(400, f"Payment exceeds the outstanding balance of KSh {f(sale.total - sale.amount_paid):,.2f}")
    sale.amount_paid = money(sale.amount_paid + amount)
    db.add(Payment(business_id=user.business_id, sale_id=sale.id, user_id=user.id, amount=amount,
                   method=data.payment_method, external_ref=data.payment_ref.upper()))
    audit(db, user, "sale.payment", "sale", sale.id, f"{amount} via {data.payment_method}")
    db.commit()
    return sale_detail(db, sale)


@router.post("/sales/{sale_id}/void")
def void_sale(sale_id: int, data: VoidIn, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    """Cancel a sale: returns items to stock and excludes it from reports. Any money taken must be refunded."""
    sale = db.scalar(select(Sale).where(Sale.id == sale_id, Sale.business_id == user.business_id).with_for_update())
    if not sale:
        raise HTTPException(404, "Sale not found")
    if sale.status == "voided":
        raise HTTPException(400, "Sale is already voided")
    items = list(db.scalars(select(SaleItem).where(SaleItem.sale_id == sale.id)))
    for item in sorted(items, key=lambda i: i.product_id):
        p = db.scalar(select(Product).where(Product.id == item.product_id, Product.business_id == user.business_id).with_for_update())
        if p:
            p.stock_qty += item.quantity
            db.add(StockMovement(business_id=user.business_id, product_id=p.id, user_id=user.id, movement_type="void",
                                 quantity=item.quantity, unit_cost=item.unit_cost, balance_after=p.stock_qty,
                                 note=f"Void {sale.reference}"))
    sale.status, sale.void_reason, sale.voided_at, sale.voided_by = "voided", data.reason, utcnow(), user.id
    audit(db, user, "sale.void", "sale", sale.id, f"{sale.reference} total {f(sale.total)} refund due {f(sale.amount_paid)}: {data.reason}")
    db.commit()
    return {**sale_detail(db, sale), "refund_due": f(sale.amount_paid)}


# ---------------------------------------------------------------- returns & refunds
class ReturnLineIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0, le=100000)


class ReturnIn(BaseModel):
    items: list[ReturnLineIn] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=3, max_length=240)
    restock: bool = True  # False for damaged/expired goods that can't be sold again
    refund_method: Literal["cash", "mpesa", "bank", "card", "other"] = "cash"


@router.post("/sales/{sale_id}/return")
def return_items(sale_id: int, data: ReturnIn, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    """Partial (or full) return. The sale is reduced so every report stays right; money already taken above the
    new total is refunded as a negative payment, which also lowers the cashier's expected cash at cash-up."""
    sale = db.scalar(select(Sale).where(Sale.id == sale_id, Sale.business_id == user.business_id).with_for_update())
    if not sale:
        raise HTTPException(404, "Sale not found")
    if sale.status != "completed":
        raise HTTPException(400, "This sale was voided")
    items = {i.product_id: i for i in db.scalars(select(SaleItem).where(SaleItem.sale_id == sale.id))}
    wanted: dict[int, int] = {}
    for line in data.items:
        wanted[line.product_id] = wanted.get(line.product_id, 0) + line.quantity
    for pid, qty in wanted.items():
        if pid not in items:
            raise HTTPException(400, "That product is not on this sale")
        if qty > items[pid].quantity:
            raise HTTPException(400, f"Only {items[pid].quantity} × {items[pid].product_name} left on this sale to return")
    value = Decimal("0")
    names = []
    for pid in sorted(wanted):
        item, qty = items[pid], wanted[pid]
        value += item.unit_price * qty
        item.quantity -= qty
        item.line_total = money(item.unit_price * item.quantity)
        names.append(f"{qty} × {item.product_name}")
        p = db.scalar(select(Product).where(Product.id == pid, Product.business_id == user.business_id).with_for_update())
        if p and data.restock:
            p.stock_qty += qty
        if p:
            db.add(StockMovement(business_id=user.business_id, product_id=p.id, user_id=user.id,
                                 movement_type="return" if data.restock else "return_damaged", quantity=qty if data.restock else 0,
                                 unit_cost=item.unit_cost, balance_after=p.stock_qty,
                                 note=f"Return {sale.reference}: {data.reason}"[:240]))
    sale.subtotal = money(sum((i.line_total for i in items.values()), Decimal("0")))
    sale.discount = min(sale.discount, sale.subtotal)
    sale.total = sale.subtotal - sale.discount
    refund = max(Decimal("0"), sale.amount_paid - sale.total)
    if refund > 0:
        sale.amount_paid -= refund
        db.add(Payment(business_id=user.business_id, sale_id=sale.id, user_id=user.id, amount=-refund,
                       method=data.refund_method, external_ref="REFUND"))
    audit(db, user, "sale.return", "sale", sale.id,
          f"{sale.reference}: {', '.join(names)} worth {f(value):.2f}; refund {f(refund):.2f} by {data.refund_method}; {data.reason}")
    db.commit()
    return {**sale_detail(db, sale), "returned_value": f(value), "refund": f(refund)}
