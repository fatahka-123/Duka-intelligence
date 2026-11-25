"""Products, stock receiving, stock counts/adjustments and movement history."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import analytics, automation, intelligence
from ..db import Business, Product, StockMovement, Supplier, User, db_session
from ..schemas import ProductIn, StockAdjustIn, StockReceiveIn, f, money, product_out
from ..security import audit, current_user, require_manager

router = APIRouter(prefix="/api")


def can_see_costs(user: User) -> bool:
    return user.role in ("owner", "manager")


def get_product(db: Session, user: User, pid: int, lock: bool = False) -> Product:
    stmt = select(Product).where(Product.id == pid, Product.business_id == user.business_id, Product.active.is_(True))
    p = db.scalar(stmt.with_for_update() if lock else stmt)
    if not p:
        raise HTTPException(404, "Product not found")
    return p


def check_unique_codes(db: Session, user: User, data: ProductIn, exclude_id: int | None = None) -> None:
    for field in ("sku", "barcode"):
        value = getattr(data, field)
        if not value:
            continue
        stmt = select(Product.id).where(Product.business_id == user.business_id, Product.active.is_(True),
                                        getattr(Product, field) == value)
        if exclude_id:
            stmt = stmt.where(Product.id != exclude_id)
        if db.scalar(stmt):
            raise HTTPException(409, f"Another product already uses this {field.upper()}")


def escape_like(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/products")
def list_products(q: str = "", category: str = "", user: User = Depends(current_user), db: Session = Depends(db_session)):
    stmt = select(Product).where(Product.business_id == user.business_id, Product.active.is_(True)).order_by(Product.name)
    if q:
        like = f"%{escape_like(q.strip())}%"
        stmt = stmt.where(or_(Product.name.ilike(like, escape="\\"), Product.sku.ilike(like, escape="\\"), Product.barcode == q.strip()))
    if category:
        stmt = stmt.where(Product.category == category)
    show = can_see_costs(user)
    return [product_out(p, show) for p in db.scalars(stmt)]


@router.get("/products/lookup")
def lookup_product(code: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    """Exact barcode / SKU match — used by barcode scanners at the till."""
    code = code.strip()
    p = db.scalar(select(Product).where(Product.business_id == user.business_id, Product.active.is_(True),
                                        or_(Product.barcode == code, Product.sku == code)))
    if not p or not code:
        raise HTTPException(404, f"No product with code {code}")
    return product_out(p, can_see_costs(user))


@router.get("/products/categories")
def categories(user: User = Depends(current_user), db: Session = Depends(db_session)):
    return sorted({c for c in db.scalars(select(Product.category).where(Product.business_id == user.business_id, Product.active.is_(True)))})


@router.get("/products/summary")
def inventory_summary(user: User = Depends(require_manager), db: Session = Depends(db_session)):
    return {**analytics.inventory_value(db, user.business_id),
            "restock": analytics.restock_plan(db, user.business_id),
            "dead_stock": analytics.dead_stock(db, user.business_id)}


@router.post("/products")
def create_product(data: ProductIn, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    check_unique_codes(db, user, data)
    p = Product(business_id=user.business_id, **data.model_dump())
    p.cost_price, p.selling_price = money(data.cost_price), money(data.selling_price)
    db.add(p)
    db.flush()
    if p.stock_qty:
        db.add(StockMovement(business_id=user.business_id, product_id=p.id, user_id=user.id, movement_type="opening",
                             quantity=p.stock_qty, unit_cost=p.cost_price, balance_after=p.stock_qty, note="Opening stock"))
    audit(db, user, "product.create", "product", p.id, p.name)
    db.commit()
    return product_out(p)


@router.put("/products/{pid}")
def update_product(pid: int, data: ProductIn, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    """Edit details and prices. Stock is NOT changed here — use receive/adjust so every change is in the ledger."""
    p = get_product(db, user, pid)
    check_unique_codes(db, user, data, exclude_id=pid)
    before = f"{f(p.cost_price)}/{f(p.selling_price)}"
    for k, v in data.model_dump(exclude={"stock_qty"}).items():
        setattr(p, k, v)
    p.cost_price, p.selling_price = money(data.cost_price), money(data.selling_price)
    audit(db, user, "product.update", "product", p.id, f"prices {before} -> {f(p.cost_price)}/{f(p.selling_price)}")
    db.commit()
    return product_out(p)


@router.delete("/products/{pid}")
def archive_product(pid: int, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    p = get_product(db, user, pid)
    p.active = False
    audit(db, user, "product.archive", "product", p.id, p.name)
    db.commit()
    return {"ok": True}


@router.post("/products/{pid}/stock")
def receive_stock(pid: int, data: StockReceiveIn, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    p = get_product(db, user, pid, lock=True)
    if data.supplier_id and not db.scalar(select(Supplier.id).where(Supplier.id == data.supplier_id, Supplier.business_id == user.business_id)):
        raise HTTPException(404, "Supplier not found")
    unit_cost = money(data.unit_cost) if data.unit_cost is not None else p.cost_price
    suggestion = intelligence.price_suggestion(p.cost_price, unit_cost, p.selling_price)
    if data.unit_cost is not None and data.update_cost_price and p.stock_qty + data.quantity > 0:
        # Weighted-average cost keeps COGS honest when buying prices change
        existing = max(p.stock_qty, 0)
        p.cost_price = money((p.cost_price * existing + unit_cost * data.quantity) / (existing + data.quantity))
    p.stock_qty += data.quantity
    db.add(StockMovement(business_id=user.business_id, product_id=p.id, user_id=user.id, supplier_id=data.supplier_id,
                         movement_type="purchase", quantity=data.quantity, unit_cost=unit_cost, balance_after=p.stock_qty, note=data.note))
    b = db.get(Business, user.business_id)
    if suggestion and b.price_alerts:
        automation.enqueue(db, b, "price_alert", f"{p.name}: buying price up {suggestion['cost_change_pct']}%",
                           f"Your margin fell from {suggestion['old_margin_pct']}% to {suggestion['new_margin_pct']}%. "
                           f"Selling at KSh {suggestion['suggested_price']:,.0f} keeps your usual margin.")
    db.commit()
    return {**product_out(p), "price_suggestion": suggestion}


@router.post("/products/{pid}/adjust")
def adjust_stock(pid: int, data: StockAdjustIn, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    """Stock count correction or write-off (damaged, expired, theft...)."""
    if (data.counted_qty is None) == (data.change is None):
        raise HTTPException(400, "Provide either counted_qty or change")
    p = get_product(db, user, pid, lock=True)
    delta = (data.counted_qty - p.stock_qty) if data.counted_qty is not None else data.change
    if delta == 0:
        return product_out(p)
    if p.stock_qty + delta < 0:
        raise HTTPException(400, f"Stock cannot go below zero (currently {p.stock_qty})")
    p.stock_qty += delta
    note = f"{data.reason}: {data.note}".strip(": ")
    db.add(StockMovement(business_id=user.business_id, product_id=p.id, user_id=user.id, movement_type="adjustment",
                         quantity=delta, unit_cost=p.cost_price, balance_after=p.stock_qty, note=note))
    audit(db, user, "stock.adjust", "product", p.id, f"{delta:+d} ({note}); value {f(p.cost_price * delta):.2f}")
    db.commit()
    return product_out(p)


@router.get("/products/{pid}/movements")
def movements(pid: int, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    get_product(db, user, pid)
    names = {u.id: u.name for u in db.scalars(select(User).where(User.business_id == user.business_id))}
    rows = db.scalars(select(StockMovement).where(StockMovement.product_id == pid, StockMovement.business_id == user.business_id)
                      .order_by(StockMovement.created_at.desc(), StockMovement.id.desc()).limit(200))
    return [{"id": m.id, "type": m.movement_type, "quantity": m.quantity, "balance_after": m.balance_after,
             "unit_cost": f(m.unit_cost) if m.unit_cost is not None else None, "note": m.note,
             "user": names.get(m.user_id), "created_at": m.created_at.isoformat()} for m in rows]
