"""Purchase orders: order from a supplier (or from the smart restock plan), send it on WhatsApp, receive it into stock."""
from __future__ import annotations

import secrets
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import analytics
from ..db import Business, Product, PurchaseOrder, PurchaseOrderItem, Supplier, User, db_session, utcnow
from ..schemas import StockReceiveIn, f, money
from ..security import audit, require_manager
from . import inventory as inventory_routes

router = APIRouter(prefix="/api")


class POLineIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0, le=1_000_000)
    unit_cost: Optional[Decimal] = Field(default=None, ge=0)


class POIn(BaseModel):
    supplier_id: Optional[int] = None
    items: list[POLineIn] = Field(min_length=1, max_length=300)
    notes: str = Field(default="", max_length=240)


class ReceiveLineIn(BaseModel):
    item_id: int
    quantity: int = Field(ge=0, le=1_000_000)
    unit_cost: Optional[Decimal] = Field(default=None, ge=0)


class POReceiveIn(BaseModel):
    items: Optional[list[ReceiveLineIn]] = None  # None = everything arrived as ordered


def po_out(db: Session, po: PurchaseOrder, with_items: bool = True) -> dict:
    sup = db.get(Supplier, po.supplier_id) if po.supplier_id else None
    out = {"id": po.id, "reference": po.reference, "status": po.status, "supplier_id": po.supplier_id,
           "supplier": sup.name if sup else None, "supplier_phone": sup.phone if sup else "", "notes": po.notes,
           "total": f(po.total), "created_at": po.created_at.isoformat(),
           "sent_at": po.sent_at.isoformat() if po.sent_at else None, "received_at": po.received_at.isoformat() if po.received_at else None}
    if with_items:
        out["items"] = [{"id": i.id, "product_id": i.product_id, "name": i.product_name, "quantity": i.quantity,
                         "unit_cost": f(i.unit_cost), "line_total": f(i.unit_cost * i.quantity), "received_qty": i.received_qty}
                        for i in db.scalars(select(PurchaseOrderItem).where(PurchaseOrderItem.order_id == po.id).order_by(PurchaseOrderItem.id))]
    return out


def get_po(db: Session, user: User, oid: int, lock: bool = False) -> PurchaseOrder:
    stmt = select(PurchaseOrder).where(PurchaseOrder.id == oid, PurchaseOrder.business_id == user.business_id)
    po = db.scalar(stmt.with_for_update() if lock else stmt)
    if not po:
        raise HTTPException(404, "Purchase order not found")
    return po


@router.get("/purchase-orders")
def list_pos(status: Optional[str] = None, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    stmt = select(PurchaseOrder).where(PurchaseOrder.business_id == user.business_id)
    if status:
        stmt = stmt.where(PurchaseOrder.status == status)
    return [po_out(db, po) for po in db.scalars(stmt.order_by(PurchaseOrder.created_at.desc()).limit(200))]


@router.get("/purchase-orders/suggest")
def suggest_po(user: User = Depends(require_manager), db: Session = Depends(db_session)):
    """Lines from the smart restock plan, ready to become an order."""
    return [{"product_id": r["product_id"], "name": r["name"], "quantity": r["suggested_order"], "stock_qty": r["stock_qty"],
             "days_left": r["days_left"], "unit_cost": f(db.get(Product, r["product_id"]).cost_price)}
            for r in analytics.restock_plan(db, user.business_id) if r["suggested_order"] > 0]


@router.post("/purchase-orders")
def create_po(data: POIn, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    bid = user.business_id
    if data.supplier_id and not db.scalar(select(Supplier.id).where(Supplier.id == data.supplier_id, Supplier.business_id == bid)):
        raise HTTPException(404, "Supplier not found")
    po = PurchaseOrder(business_id=bid, supplier_id=data.supplier_id, user_id=user.id, notes=data.notes, status="draft",
                       reference="PO-" + secrets.token_hex(3).upper(), total=Decimal("0"))
    db.add(po)
    db.flush()
    total = Decimal("0")
    merged: dict[int, POLineIn] = {}
    for line in data.items:
        if line.product_id in merged:
            merged[line.product_id].quantity += line.quantity
        else:
            merged[line.product_id] = line.model_copy()
    for pid, line in merged.items():
        p = db.scalar(select(Product).where(Product.id == pid, Product.business_id == bid))
        if not p:
            raise HTTPException(404, f"Product {pid} not found")
        cost = money(line.unit_cost if line.unit_cost is not None else p.cost_price)
        db.add(PurchaseOrderItem(order_id=po.id, product_id=p.id, product_name=p.name, quantity=line.quantity, unit_cost=cost))
        total += cost * line.quantity
    po.total = money(total)
    audit(db, user, "po.create", "purchase_order", po.id, f"{po.reference} {f(po.total):.2f}")
    db.commit()
    return po_out(db, po)


@router.post("/purchase-orders/{oid}/sent")
def mark_sent(oid: int, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    po = get_po(db, user, oid, lock=True)
    if po.status not in ("draft", "sent"):
        raise HTTPException(400, f"This order is already {po.status}")
    po.status, po.sent_at = "sent", po.sent_at or utcnow()
    db.commit()
    return po_out(db, po)


@router.post("/purchase-orders/{oid}/receive")
def receive_po(oid: int, data: POReceiveIn, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    """Book the delivery into stock through the normal receiving path (weighted cost, price coach alerts)."""
    po = get_po(db, user, oid, lock=True)
    if po.status in ("received", "cancelled"):
        raise HTTPException(400, f"This order is already {po.status}")
    items = {i.id: i for i in db.scalars(select(PurchaseOrderItem).where(PurchaseOrderItem.order_id == po.id))}
    lines = data.items if data.items is not None else [ReceiveLineIn(item_id=i.id, quantity=i.quantity) for i in items.values()]
    if not any(l.quantity > 0 for l in lines):
        raise HTTPException(400, "Enter at least one quantity received")
    for l in lines:  # validate everything before any stock moves
        if l.item_id not in items:
            raise HTTPException(400, "That line is not on this order")
    received_total = Decimal("0")
    for l in lines:
        item = items[l.item_id]
        if l.quantity == 0:
            continue
        cost = money(l.unit_cost) if l.unit_cost is not None else item.unit_cost
        inventory_routes.receive_stock(item.product_id, StockReceiveIn(quantity=l.quantity, unit_cost=cost, supplier_id=po.supplier_id,
                                                                       note=f"PO {po.reference}"), user, db)
        item.received_qty, item.unit_cost = l.quantity, cost
        received_total += cost * l.quantity
    po.total = money(received_total)
    po.status, po.received_at = "received", utcnow()
    audit(db, user, "po.receive", "purchase_order", po.id, f"{po.reference} received {f(po.total):.2f}")
    db.commit()
    return po_out(db, po)


@router.post("/purchase-orders/{oid}/cancel")
def cancel_po(oid: int, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    po = get_po(db, user, oid, lock=True)
    if po.status == "received":
        raise HTTPException(400, "A received order can't be cancelled — use a stock adjustment instead")
    po.status = "cancelled"
    audit(db, user, "po.cancel", "purchase_order", po.id, po.reference)
    db.commit()
    return po_out(db, po)


@router.get("/purchase-orders/{oid}/message")
def po_message(oid: int, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    """Plain-text order for WhatsApp/SMS."""
    po = get_po(db, user, oid)
    b = db.get(Business, user.business_id)
    out = po_out(db, po)
    lines = [f"Habari{', ' + out['supplier'] if out['supplier'] else ''}. Order {po.reference} from {b.name}:", ""]
    lines += [f"• {i['name']} — {i['quantity']}" for i in out["items"]]
    if po.notes:
        lines += ["", po.notes]
    lines += ["", f"Please confirm price & delivery. {b.phone or ''}".strip(), "Asante."]
    return {"text": "\n".join(lines), "phone": out["supplier_phone"]}
