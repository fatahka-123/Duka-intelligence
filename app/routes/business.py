"""Customers, suppliers, expenses, dashboard, reports, insights, assistant and CSV exports."""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import analytics
from ..db import Business, Customer, Expense, Payment, Product, Sale, StockMovement, Supplier, User, db_session, utcnow
from ..schemas import (
    AssistantIn, CustomerIn, ExpenseIn, SupplierIn, customer_out, expense_out, f, money, product_out, sale_out, supplier_out,
)
from ..security import audit, current_user, require_manager, require_owner

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------- customers
def balances(db: Session, bid: int) -> dict[int, float]:
    return dict(db.execute(select(Sale.customer_id, func.sum(Sale.total - Sale.amount_paid)).where(
        Sale.business_id == bid, Sale.status == "completed", Sale.customer_id.is_not(None)).group_by(Sale.customer_id)).all())


def get_customer(db: Session, user: User, cid: int) -> Customer:
    c = db.scalar(select(Customer).where(Customer.id == cid, Customer.business_id == user.business_id))
    if not c:
        raise HTTPException(404, "Customer not found")
    return c


@router.get("/customers")
def list_customers(user: User = Depends(current_user), db: Session = Depends(db_session)):
    bal = balances(db, user.business_id)
    return [customer_out(c, bal.get(c.id, 0)) for c in
            db.scalars(select(Customer).where(Customer.business_id == user.business_id).order_by(Customer.name))]


@router.post("/customers")
def create_customer(data: CustomerIn, user: User = Depends(current_user), db: Session = Depends(db_session)):
    payload = data.model_dump()
    if user.role == "cashier":
        payload["credit_limit"] = 0  # only managers set credit limits
    c = Customer(business_id=user.business_id, **payload)
    db.add(c)
    db.commit()
    return customer_out(c)


@router.put("/customers/{cid}")
def update_customer(cid: int, data: CustomerIn, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    c = get_customer(db, user, cid)
    for k, v in data.model_dump().items():
        setattr(c, k, v)
    db.commit()
    return customer_out(c, balances(db, user.business_id).get(c.id, 0))


@router.get("/customers/{cid}/statement")
def customer_statement(cid: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    c = get_customer(db, user, cid)
    sales = list(db.scalars(select(Sale).where(Sale.business_id == user.business_id, Sale.customer_id == cid)
                            .order_by(Sale.created_at.desc()).limit(200)))
    completed = [s for s in sales if s.status == "completed"]
    return {
        "customer": customer_out(c, sum((s.total - s.amount_paid for s in completed), 0)),
        "lifetime_spend": f(sum((s.total for s in completed), 0)),
        "visits": len(completed),
        "last_visit": completed[0].created_at.isoformat() if completed else None,
        "sales": [sale_out(s, c.name) for s in sales],
    }


# ---------------------------------------------------------------- suppliers
@router.get("/suppliers")
def list_suppliers(user: User = Depends(current_user), db: Session = Depends(db_session)):
    return [supplier_out(s) for s in db.scalars(select(Supplier).where(Supplier.business_id == user.business_id).order_by(Supplier.name))]


@router.post("/suppliers")
def create_supplier(data: SupplierIn, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    s = Supplier(business_id=user.business_id, **data.model_dump())
    db.add(s)
    db.commit()
    return supplier_out(s)


@router.put("/suppliers/{sid}")
def update_supplier(sid: int, data: SupplierIn, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    s = db.scalar(select(Supplier).where(Supplier.id == sid, Supplier.business_id == user.business_id))
    if not s:
        raise HTTPException(404, "Supplier not found")
    for k, v in data.model_dump().items():
        setattr(s, k, v)
    db.commit()
    return supplier_out(s)


@router.get("/suppliers/{sid}/purchases")
def supplier_purchases(sid: int, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    rows = db.execute(select(StockMovement, Product.name).join(Product, Product.id == StockMovement.product_id).where(
        StockMovement.business_id == user.business_id, StockMovement.supplier_id == sid)
        .order_by(StockMovement.created_at.desc()).limit(200)).all()
    return [{"product": name, "quantity": m.quantity, "unit_cost": f(m.unit_cost), "total": f((m.unit_cost or 0) * m.quantity),
             "note": m.note, "created_at": m.created_at.isoformat()} for m, name in rows]


# ---------------------------------------------------------------- expenses
@router.get("/expenses")
def list_expenses(start: Optional[date] = None, end: Optional[date] = None, user: User = Depends(require_manager),
                  db: Session = Depends(db_session)):
    stmt = select(Expense).where(Expense.business_id == user.business_id)
    if start or end:
        b = db.get(Business, user.business_id)
        p = analytics.resolve_period(b, None, start or end, end or start)
        lo, hi = p.utc_bounds(analytics.tz_for(b))
        stmt = stmt.where(Expense.created_at >= lo, Expense.created_at < hi)
    return [expense_out(x) for x in db.scalars(stmt.order_by(Expense.created_at.desc()).limit(500))]


@router.post("/expenses")
def create_expense(data: ExpenseIn, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    x = Expense(business_id=user.business_id, user_id=user.id, category=data.category, description=data.description, amount=money(data.amount))
    db.add(x)
    db.commit()
    return expense_out(x)


@router.delete("/expenses/{eid}")
def delete_expense(eid: int, user: User = Depends(require_manager), db: Session = Depends(db_session)):
    x = db.scalar(select(Expense).where(Expense.id == eid, Expense.business_id == user.business_id))
    if not x:
        raise HTTPException(404, "Expense not found")
    audit(db, user, "expense.delete", "expense", x.id, f"{x.category} {f(x.amount)} {x.description}")
    db.delete(x)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- dashboard / reports / intelligence
@router.get("/dashboard")
def dashboard(user: User = Depends(current_user), db: Session = Depends(db_session)):
    b = db.get(Business, user.business_id)
    bid, tz = b.id, analytics.tz_for(b)
    today = analytics.local_today(b)
    t_lo, t_hi = analytics.Period(today, today, "today").utc_bounds(tz)
    y_lo, y_hi = analytics.Period(today - timedelta(days=1), today - timedelta(days=1), "yesterday").utc_bounds(tz)
    cur, yest = analytics.sales_totals(db, bid, t_lo, t_hi), analytics.sales_totals(db, bid, y_lo, y_hi)
    collected = db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).join(Sale, Sale.id == Payment.sale_id).where(
        Payment.business_id == bid, Sale.status == "completed", Payment.created_at >= t_lo, Payment.created_at < t_hi))
    products = list(db.scalars(select(Product).where(Product.business_id == bid, Product.active.is_(True))))
    show = user.role != "cashier"
    recent_stmt = select(Sale).where(Sale.business_id == bid)
    if not show:
        recent_stmt = recent_stmt.where(Sale.user_id == user.id)
    recent = list(db.scalars(recent_stmt.order_by(Sale.created_at.desc()).limit(8)))
    out = {
        "date": today.isoformat(),
        "today_revenue": f(cur["revenue"]), "today_sales": cur["count"],
        "yesterday_revenue": f(yest["revenue"]), "revenue_change": analytics.pct_change(cur["revenue"], yest["revenue"]),
        "today_collected": f(collected),
        "product_count": len(products),
        "low_stock": [product_out(p, show) for p in sorted(products, key=lambda p: p.stock_qty) if p.stock_qty <= p.reorder_level][:8],
        "outstanding_balance": f(analytics.outstanding_balance(db, bid)),
        "recent_sales": [sale_out(s) for s in recent],
        "last_7_days": analytics.daily_series(db, bid, analytics.named_period("7d", today), tz),
    }
    if show:
        out["today_gross_profit"] = f(cur["revenue"] - cur["cogs"])
        out["today_expenses"] = f(analytics.expense_total(db, bid, t_lo, t_hi))
        out["insights"] = analytics.insights(db, b)[:4]
    return out


@router.get("/reports/summary")
def report_summary(period: str = "30d", start: Optional[date] = None, end: Optional[date] = None,
                   user: User = Depends(require_manager), db: Session = Depends(db_session)):
    b = db.get(Business, user.business_id)
    return analytics.summary_report(db, b, analytics.resolve_period(b, period, start, end))


@router.get("/reports/profit")
def profit_report(user: User = Depends(require_manager), db: Session = Depends(db_session)):
    """Kept for v0.1 API compatibility; prefer /reports/summary."""
    b = db.get(Business, user.business_id)
    r = analytics.summary_report(db, b, analytics.named_period("30d", analytics.local_today(b)))
    return {"period_days": 30, **{k: r[k] for k in ("revenue", "cost_of_goods_sold", "gross_profit", "operating_expenses",
                                                     "estimated_net_operating_profit", "transactions")}}


@router.get("/insights")
def get_insights(user: User = Depends(require_manager), db: Session = Depends(db_session)):
    return analytics.insights(db, db.get(Business, user.business_id))


@router.get("/debtors")
def get_debtors(user: User = Depends(current_user), db: Session = Depends(db_session)):
    return analytics.debtors(db, user.business_id)


@router.post("/assistant")
def assistant(data: AssistantIn, user: User = Depends(current_user), db: Session = Depends(db_session)):
    return analytics.answer_question(db, db.get(Business, user.business_id), data.question, can_see_costs=user.role != "cashier")


# ---------------------------------------------------------------- exports
def _safe_cell(v):
    """Neutralise spreadsheet formula injection (=, +, -, @ at the start of text)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def csv_response(filename: str, header: list[str], rows) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([_safe_cell(v) for v in r])
    return StreamingResponse(iter(["﻿" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/export/backup.json")
def export_backup(user: User = Depends(require_owner), db: Session = Depends(db_session)):
    """Everything this shop has recorded, in one file the owner can keep safe (API keys and passwords excluded)."""
    import json
    from datetime import datetime
    from decimal import Decimal as D
    from ..db import MpesaTransaction, PurchaseOrder, PurchaseOrderItem, SaleItem, Shift

    bid = user.business_id

    def rows(model, *where):
        out = []
        for r in db.scalars(select(model).where(*where)):
            d = {}
            for c in model.__table__.columns:
                if c.name in ("password_hash", "reset_code_hash", "raw") or c.name.startswith("mpesa_consumer") or c.name in ("mpesa_passkey", "mpesa_callback_token"):
                    continue
                v = getattr(r, c.name)
                d[c.name] = v.isoformat() if isinstance(v, datetime) else float(v) if isinstance(v, D) else v
            out.append(d)
        return out

    sale_ids = select(Sale.id).where(Sale.business_id == bid)
    order_ids = select(PurchaseOrder.id).where(PurchaseOrder.business_id == bid)
    data = {
        "exported_at": utcnow().isoformat() + "Z", "format": "duka-backup-1",
        "business": rows(Business, Business.id == bid)[0],
        "staff": rows(User, User.business_id == bid),
        "products": rows(Product, Product.business_id == bid),
        "customers": rows(Customer, Customer.business_id == bid),
        "suppliers": rows(Supplier, Supplier.business_id == bid),
        "sales": rows(Sale, Sale.business_id == bid),
        "sale_items": rows(SaleItem, SaleItem.sale_id.in_(sale_ids)),
        "payments": rows(Payment, Payment.business_id == bid),
        "expenses": rows(Expense, Expense.business_id == bid),
        "stock_movements": rows(StockMovement, StockMovement.business_id == bid),
        "mpesa_transactions": rows(MpesaTransaction, MpesaTransaction.business_id == bid),
        "shifts": rows(Shift, Shift.business_id == bid),
        "purchase_orders": rows(PurchaseOrder, PurchaseOrder.business_id == bid),
        "purchase_order_items": rows(PurchaseOrderItem, PurchaseOrderItem.order_id.in_(order_ids)),
    }
    audit(db, user, "export.backup", "business", bid)
    db.commit()
    stamp = utcnow().strftime("%Y%m%d")
    return StreamingResponse(iter([json.dumps(data, ensure_ascii=False)]), media_type="application/json",
                             headers={"Content-Disposition": f'attachment; filename="duka-backup-{stamp}.json"'})


@router.get("/export/{kind}.csv")
def export_csv(kind: str, period: str = "90d", start: Optional[date] = None, end: Optional[date] = None,
               user: User = Depends(require_manager), db: Session = Depends(db_session)):
    b = db.get(Business, user.business_id)
    p = analytics.resolve_period(b, period, start, end)
    tz = analytics.tz_for(b)
    lo, hi = p.utc_bounds(tz)
    stamp = f"{p.start:%Y%m%d}-{p.end:%Y%m%d}"
    local = lambda dt: analytics.to_local(dt, tz).strftime("%Y-%m-%d %H:%M")
    if kind == "sales":
        customers = dict(db.execute(select(Customer.id, Customer.name).where(Customer.business_id == b.id)).all())
        rows = db.scalars(select(Sale).where(Sale.business_id == b.id, Sale.created_at >= lo, Sale.created_at < hi).order_by(Sale.created_at))
        return csv_response(f"sales-{stamp}.csv", ["Date", "Reference", "Customer", "Subtotal", "Discount", "Total", "Paid", "Balance", "Method", "Status"],
                            ([local(s.created_at), s.reference, customers.get(s.customer_id, "Walk-in"), f(s.subtotal), f(s.discount),
                              f(s.total), f(s.amount_paid), f(s.total - s.amount_paid), s.payment_method, s.status] for s in rows))
    if kind == "products":
        rows = db.scalars(select(Product).where(Product.business_id == b.id, Product.active.is_(True)).order_by(Product.name))
        return csv_response("products.csv", ["Name", "SKU", "Barcode", "Category", "Unit", "Cost", "Price", "Stock", "Reorder level", "Stock value (cost)"],
                            ([x.name, x.sku, x.barcode, x.category, x.unit, f(x.cost_price), f(x.selling_price), x.stock_qty,
                              x.reorder_level, f(x.cost_price * x.stock_qty)] for x in rows))
    if kind == "expenses":
        rows = db.scalars(select(Expense).where(Expense.business_id == b.id, Expense.created_at >= lo, Expense.created_at < hi).order_by(Expense.created_at))
        return csv_response(f"expenses-{stamp}.csv", ["Date", "Category", "Description", "Amount"],
                            ([local(x.created_at), x.category, x.description, f(x.amount)] for x in rows))
    if kind == "payments":
        refs = dict(db.execute(select(Sale.id, Sale.reference).where(Sale.business_id == b.id)).all())
        rows = db.scalars(select(Payment).where(Payment.business_id == b.id, Payment.created_at >= lo, Payment.created_at < hi).order_by(Payment.created_at))
        return csv_response(f"payments-{stamp}.csv", ["Date", "Sale", "Amount", "Method", "Reference"],
                            ([local(x.created_at), refs.get(x.sale_id), f(x.amount), x.method, x.external_ref] for x in rows))
    raise HTTPException(404, "Unknown export")
