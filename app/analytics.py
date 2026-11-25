"""Reporting, proactive insights and the rule-based (English + Kiswahili) assistant.

All figures are computed from recorded data only. Day boundaries use the business
timezone (Kenya is UTC+3, so 'today' must not be UTC midnight).
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from .db import Business, Customer, Expense, Payment, Product, Sale, SaleItem, utcnow

ZERO = Decimal("0")


def f(v) -> float:
    return float(v or 0)


def ksh(v) -> str:
    return f"KSh {f(v):,.2f}"


# ---------------------------------------------------------------- time helpers
def tz_for(business: Business) -> ZoneInfo:
    try:
        return ZoneInfo(business.timezone or "Africa/Nairobi")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Africa/Nairobi")


def local_today(business: Business) -> date:
    return datetime.now(tz_for(business)).date()


def to_utc_naive(d: date, tz: ZoneInfo) -> datetime:
    """Local midnight of ``d`` expressed as naive UTC (DB storage format)."""
    return datetime.combine(d, time.min, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


def to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(tz)


@dataclass
class Period:
    start: date  # inclusive, local
    end: date  # inclusive, local
    label: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def previous(self) -> "Period":
        span = timedelta(days=self.days)
        return Period(self.start - span, self.end - span, "previous period")

    def utc_bounds(self, tz: ZoneInfo) -> tuple[datetime, datetime]:
        return to_utc_naive(self.start, tz), to_utc_naive(self.end + timedelta(days=1), tz)


def named_period(name: str, today: date) -> Period:
    name = (name or "30d").lower()
    if name == "today":
        return Period(today, today, "today")
    if name == "yesterday":
        y = today - timedelta(days=1)
        return Period(y, y, "yesterday")
    if name in ("week", "this_week"):
        return Period(today - timedelta(days=today.weekday()), today, "this week")
    if name in ("month", "this_month"):
        return Period(today.replace(day=1), today, "this month")
    if name == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        return Period(last.replace(day=1), last, "last month")
    m = re.fullmatch(r"(\d{1,3})d", name)
    n = int(m.group(1)) if m else 30
    n = max(1, min(n, 366))
    return Period(today - timedelta(days=n - 1), today, f"last {n} days")


def resolve_period(business: Business, period: str | None, start: date | None, end: date | None) -> Period:
    today = local_today(business)
    if start and end:
        if end < start:
            start, end = end, start
        if (end - start).days > 366:
            start = end - timedelta(days=366)
        return Period(start, end, f"{start:%d %b %Y} – {end:%d %b %Y}")
    return named_period(period or "30d", today)


# ---------------------------------------------------------------- core aggregates
def sales_totals(db: Session, bid: int, lo: datetime, hi: datetime) -> dict:
    """Revenue, transaction count, discount and COGS for completed sales in [lo, hi)."""
    where = and_(Sale.business_id == bid, Sale.status == "completed", Sale.created_at >= lo, Sale.created_at < hi)
    revenue, count, discount = db.execute(
        select(func.coalesce(func.sum(Sale.total), 0), func.count(Sale.id), func.coalesce(func.sum(Sale.discount), 0)).where(where)
    ).one()
    cogs = db.scalar(
        select(func.coalesce(func.sum(SaleItem.unit_cost * SaleItem.quantity), 0)).join(Sale, Sale.id == SaleItem.sale_id).where(where)
    )
    return {"revenue": Decimal(revenue), "count": int(count), "discount": Decimal(discount), "cogs": Decimal(cogs)}


def expense_total(db: Session, bid: int, lo: datetime, hi: datetime) -> Decimal:
    return Decimal(db.scalar(select(func.coalesce(func.sum(Expense.amount), 0)).where(
        Expense.business_id == bid, Expense.created_at >= lo, Expense.created_at < hi)))


def outstanding_balance(db: Session, bid: int) -> Decimal:
    return Decimal(db.scalar(select(func.coalesce(func.sum(Sale.total - Sale.amount_paid), 0)).where(
        Sale.business_id == bid, Sale.status == "completed")))


def pct_change(cur: Decimal | float, prev: Decimal | float) -> float | None:
    cur, prev = f(cur), f(prev)
    if prev == 0:
        return None
    return round((cur - prev) / abs(prev) * 100, 1)


def top_products(db: Session, bid: int, lo: datetime, hi: datetime, limit: int = 10) -> list[dict]:
    qty = func.sum(SaleItem.quantity)
    revenue = func.sum(SaleItem.line_total)
    cost = func.sum(SaleItem.unit_cost * SaleItem.quantity)
    rows = db.execute(
        select(SaleItem.product_id, SaleItem.product_name, qty.label("qty"), revenue.label("rev"), cost.label("cost"))
        .join(Sale, Sale.id == SaleItem.sale_id)
        .where(Sale.business_id == bid, Sale.status == "completed", Sale.created_at >= lo, Sale.created_at < hi)
        .group_by(SaleItem.product_id, SaleItem.product_name)
        .order_by(revenue.desc()).limit(limit)
    ).all()
    return [{"product_id": r.product_id, "name": r.product_name, "quantity": int(r.qty), "revenue": f(r.rev),
             "gross_profit": f(Decimal(r.rev) - Decimal(r.cost))} for r in rows]


def daily_series(db: Session, bid: int, period: Period, tz: ZoneInfo) -> list[dict]:
    lo, hi = period.utc_bounds(tz)
    buckets: dict[date, list] = {period.start + timedelta(days=i): [ZERO, 0] for i in range(period.days)}
    for created, total in db.execute(select(Sale.created_at, Sale.total).where(
            Sale.business_id == bid, Sale.status == "completed", Sale.created_at >= lo, Sale.created_at < hi)):
        d = to_local(created, tz).date()
        if d in buckets:
            buckets[d][0] += total
            buckets[d][1] += 1
    return [{"date": d.isoformat(), "revenue": f(v[0]), "sales": v[1]} for d, v in buckets.items()]


def summary_report(db: Session, business: Business, period: Period) -> dict:
    bid, tz = business.id, tz_for(business)
    lo, hi = period.utc_bounds(tz)
    prev = period.previous()
    plo, phi = prev.utc_bounds(tz)
    cur, before = sales_totals(db, bid, lo, hi), sales_totals(db, bid, plo, phi)
    exp, prev_exp = expense_total(db, bid, lo, hi), expense_total(db, bid, plo, phi)
    gross, prev_gross = cur["revenue"] - cur["cogs"], before["revenue"] - before["cogs"]
    net, prev_net = gross - exp, prev_gross - prev_exp

    by_method = db.execute(
        select(Payment.method, func.sum(Payment.amount)).join(Sale, Sale.id == Payment.sale_id)
        .where(Payment.business_id == bid, Sale.status == "completed", Payment.created_at >= lo, Payment.created_at < hi)
        .group_by(Payment.method)
    ).all()
    by_category = db.execute(
        select(Expense.category, func.sum(Expense.amount)).where(
            Expense.business_id == bid, Expense.created_at >= lo, Expense.created_at < hi)
        .group_by(Expense.category).order_by(func.sum(Expense.amount).desc())
    ).all()
    credit_issued = db.scalar(select(func.coalesce(func.sum(Sale.total - Sale.amount_paid), 0)).where(
        Sale.business_id == bid, Sale.status == "completed", Sale.created_at >= lo, Sale.created_at < hi))

    return {
        "period": {"start": period.start.isoformat(), "end": period.end.isoformat(), "label": period.label, "days": period.days},
        "revenue": f(cur["revenue"]),
        "transactions": cur["count"],
        "average_sale": f(cur["revenue"] / cur["count"]) if cur["count"] else 0,
        "discounts_given": f(cur["discount"]),
        "cost_of_goods_sold": f(cur["cogs"]),
        "gross_profit": f(gross),
        "gross_margin_pct": round(f(gross) / f(cur["revenue"]) * 100, 1) if cur["revenue"] else 0,
        "operating_expenses": f(exp),
        "estimated_net_operating_profit": f(net),
        "credit_issued": f(credit_issued),
        "change": {
            "revenue": pct_change(cur["revenue"], before["revenue"]),
            "transactions": pct_change(cur["count"], before["count"]),
            "gross_profit": pct_change(gross, prev_gross),
            "net_profit": pct_change(net, prev_net),
        },
        "previous": {"revenue": f(before["revenue"]), "gross_profit": f(prev_gross), "net_profit": f(prev_net)},
        "daily": daily_series(db, bid, period, tz),
        "top_products": top_products(db, bid, lo, hi),
        "payment_methods": [{"method": m, "amount": f(a)} for m, a in by_method],
        "expenses_by_category": [{"category": c, "amount": f(a)} for c, a in by_category],
    }


# ---------------------------------------------------------------- stock intelligence
def sales_velocity(db: Session, bid: int, days: int = 28) -> dict[int, float]:
    """Average units sold per day per product over the trailing window."""
    since = utcnow() - timedelta(days=days)
    rows = db.execute(
        select(SaleItem.product_id, func.sum(SaleItem.quantity)).join(Sale, Sale.id == SaleItem.sale_id)
        .where(Sale.business_id == bid, Sale.status == "completed", Sale.created_at >= since)
        .group_by(SaleItem.product_id)
    ).all()
    return {pid: int(q) / days for pid, q in rows}


def restock_plan(db: Session, bid: int, cover_days: int = 14) -> list[dict]:
    """Products that will run out within ~a week, with a suggested order to cover ``cover_days``."""
    velocity = sales_velocity(db, bid)
    plan = []
    for p in db.scalars(select(Product).where(Product.business_id == bid, Product.active.is_(True))):
        v = velocity.get(p.id, 0)
        days_left = (p.stock_qty / v) if v > 0 else None
        low = p.stock_qty <= p.reorder_level
        soon = days_left is not None and days_left <= 7
        if not (low or soon):
            continue
        suggested = max(int(round(v * cover_days)) - p.stock_qty, p.reorder_level * 2 - p.stock_qty if v == 0 else 0, 0)
        plan.append({
            "product_id": p.id, "name": p.name, "stock_qty": p.stock_qty, "reorder_level": p.reorder_level,
            "daily_sales": round(v, 2), "days_left": round(days_left, 1) if days_left is not None else None,
            "suggested_order": suggested, "estimated_cost": f(p.cost_price * suggested),
        })
    plan.sort(key=lambda x: (x["days_left"] if x["days_left"] is not None else 999, x["stock_qty"]))
    return plan


def dead_stock(db: Session, bid: int, days: int = 30) -> list[dict]:
    since = utcnow() - timedelta(days=days)
    sold_recently = select(SaleItem.product_id).join(Sale, Sale.id == SaleItem.sale_id).where(
        Sale.business_id == bid, Sale.status == "completed", Sale.created_at >= since)
    rows = db.scalars(select(Product).where(
        Product.business_id == bid, Product.active.is_(True), Product.stock_qty > 0,
        Product.created_at < since, Product.id.not_in(sold_recently)))
    out = [{"product_id": p.id, "name": p.name, "stock_qty": p.stock_qty, "capital_tied": f(p.cost_price * p.stock_qty)} for p in rows]
    return sorted(out, key=lambda x: -x["capital_tied"])


def debtors(db: Session, bid: int) -> list[dict]:
    bal = func.sum(Sale.total - Sale.amount_paid)
    rows = db.execute(
        select(Customer.id, Customer.name, Customer.phone, bal.label("balance"), func.min(Sale.created_at).label("oldest"))
        .join(Sale, Sale.customer_id == Customer.id)
        .where(Sale.business_id == bid, Customer.business_id == bid, Sale.status == "completed", Sale.total > Sale.amount_paid)
        .group_by(Customer.id, Customer.name, Customer.phone).order_by(bal.desc())
    ).all()
    now = utcnow()
    return [{"customer_id": r.id, "name": r.name, "phone": r.phone, "balance": f(r.balance),
             "oldest_days": (now - r.oldest).days} for r in rows]


def inventory_value(db: Session, bid: int) -> dict:
    cost, retail, units = db.execute(select(
        func.coalesce(func.sum(Product.cost_price * Product.stock_qty), 0),
        func.coalesce(func.sum(Product.selling_price * Product.stock_qty), 0),
        func.coalesce(func.sum(Product.stock_qty), 0),
    ).where(Product.business_id == bid, Product.active.is_(True))).one()
    return {"cost_value": f(cost), "retail_value": f(retail), "units": int(units), "potential_profit": f(Decimal(retail) - Decimal(cost))}


# ---------------------------------------------------------------- insights
def insights(db: Session, business: Business) -> list[dict]:
    """Proactive, plain-language observations ranked by urgency."""
    bid, tz = business.id, tz_for(business)
    today = local_today(business)
    out: list[dict] = []

    def add(level, title, detail, action=None, page=None):
        out.append({"level": level, "title": title, "detail": detail, "action": action, "page": page})

    # Stock-outs and imminent stock-outs
    plan = restock_plan(db, bid)
    outs = [p for p in plan if p["stock_qty"] <= 0]
    if outs:
        names = ", ".join(p["name"] for p in outs[:3])
        add("critical", f"{len(outs)} product(s) out of stock", f"{names}{'…' if len(outs) > 3 else ''} cannot be sold until restocked.",
            "View restock plan", "products")
    soon = [p for p in plan if p["stock_qty"] > 0 and p["days_left"] is not None and p["days_left"] <= 5]
    if soon:
        p = soon[0]
        add("warning", f"{p['name']} runs out in ~{p['days_left']:.0f} day(s)",
            f"You sell about {p['daily_sales']:g} a day and have {p['stock_qty']} left. Suggested order: {p['suggested_order']} "
            f"(≈ {ksh(p['estimated_cost'])}).", "View restock plan", "products")

    # Selling at a loss
    losers = list(db.scalars(select(Product).where(Product.business_id == bid, Product.active.is_(True),
                                                   Product.selling_price < Product.cost_price)))
    if losers:
        add("critical", f"{len(losers)} product(s) priced below cost",
            "Every sale of " + ", ".join(p.name for p in losers[:3]) + " loses money. Review prices.", "Review prices", "products")

    # Revenue trend: last 7 days vs previous 7
    week = Period(today - timedelta(days=6), today, "last 7 days")
    lo, hi = week.utc_bounds(tz)
    plo, phi = week.previous().utc_bounds(tz)
    cur, prev = sales_totals(db, bid, lo, hi), sales_totals(db, bid, plo, phi)
    ch = pct_change(cur["revenue"], prev["revenue"])
    if ch is not None and abs(ch) >= 10:
        up = ch > 0
        add("good" if up else "warning", f"Sales {'up' if up else 'down'} {abs(ch):.0f}% this week",
            f"{ksh(cur['revenue'])} in the last 7 days vs {ksh(prev['revenue'])} the week before.", "Open reports", "reports")

    # Debtors
    owed = debtors(db, bid)
    if owed:
        total = sum(d["balance"] for d in owed)
        old = [d for d in owed if d["oldest_days"] >= 14]
        detail = f"{len(owed)} customer(s) owe you {ksh(total)}."
        if old:
            detail += f" {len(old)} have balances older than 2 weeks — {old[0]['name']} owes {ksh(old[0]['balance'])}."
        add("warning" if old else "info", "Money owed to you", detail, "Follow up", "customers")

    # Dead stock
    dead = dead_stock(db, bid)
    if dead:
        tied = sum(d["capital_tied"] for d in dead)
        add("info", f"{ksh(tied)} tied up in slow stock",
            f"{len(dead)} product(s) have not sold in 30 days, e.g. {dead[0]['name']}. Consider a promotion or bundle.",
            "See products", "products")

    # Best trading day of week (needs at least 3 weeks of history)
    since = to_utc_naive(today - timedelta(days=55), tz)
    dow = defaultdict(lambda: ZERO)
    first = None
    for created, total in db.execute(select(Sale.created_at, Sale.total).where(
            Sale.business_id == bid, Sale.status == "completed", Sale.created_at >= since)):
        local = to_local(created, tz)
        first = local if first is None or local < first else first
        dow[local.strftime("%A")] += total
    if first and (datetime.now(tz) - first).days >= 21 and dow:
        best = max(dow, key=dow.get)
        add("info", f"{best} is your strongest day", f"Plan stock deliveries and staffing ahead of {best}s.", None, None)

    # Expense ratio this month
    month = named_period("month", today)
    mlo, mhi = month.utc_bounds(tz)
    m = sales_totals(db, bid, mlo, mhi)
    m_exp = expense_total(db, bid, mlo, mhi)
    gross = m["revenue"] - m["cogs"]
    if m_exp > 0 and gross > 0 and m_exp > gross:
        add("critical", "Expenses exceed gross profit this month",
            f"Gross profit {ksh(gross)} vs expenses {ksh(m_exp)}. You are currently operating at a loss.", "Open reports", "reports")

    if not out:
        add("good", "All clear", "No stock, pricing or credit issues detected. Keep recording sales and expenses for sharper insights.")
    rank = {"critical": 0, "warning": 1, "good": 2, "info": 3}
    return sorted(out, key=lambda x: rank[x["level"]])


# ---------------------------------------------------------------- assistant
INTENTS: list[tuple[str, tuple[str, ...]]] = [
    ("restock", ("restock", "reorder", "low stock", "running out", "order more", "what should i buy", "what to buy",
                 "stock ya chini", "zinaisha", "imeisha", "niagize", "kuagiza", "nunue")),
    ("debtors", ("owe", "owes", "debt", "debtor", "credit", "balance", "unpaid", "deni", "madeni", "anadaiwa", "wanadaiwa", "wanaodaiwa", "ananidai", "wananidai", "nani anadai")),
    ("dead", ("slow", "not selling", "dead stock", "haziuziki", "hazinunuliwi")),
    ("top", ("top", "best sell", "best-sell", "fastest", "most popular", "sells most", "zinauzika", "inauzika", "bora")),
    ("expenses", ("expense", "spent", "spend", "costs", "matumizi", "gharama")),
    ("profit", ("profit", "margin", "made", "earn", "faida", "nimepata")),
    ("sales", ("sales", "sale", "sell", "revenue", "sold", "takings", "turnover", "mauzo", "nimeuza", "mapato")),
    ("value", ("inventory value", "stock value", "worth", "thamani")),
    ("insights", ("advice", "insight", "how am i doing", "summary", "ushauri", "hali ya biashara", "muhtasari")),
]

PERIOD_WORDS = [
    ("yesterday", ("yesterday", "jana")),
    ("today", ("today", "leo")),
    ("last_month", ("last month", "mwezi uliopita")),
    ("month", ("this month", "month", "mwezi huu", "mwezi")),
    ("week", ("this week", "week", "wiki hii", "wiki")),
]


def detect_period(q: str, today: date) -> Period:
    m = re.search(r"(?:last|past|siku)\s*(\d{1,3})\s*(?:days|siku)?", q)
    if m:
        return named_period(f"{m.group(1)}d", today)
    for name, words in PERIOD_WORDS:
        if any(re.search(rf"\b{re.escape(w)}\b", q) for w in words):
            return named_period(name, today)
    return named_period("30d", today)


def is_swahili(q: str) -> bool:
    sw = ("nani", "zipi", "ipi", "leo", "jana", "faida", "mauzo", "deni", "bidhaa", "wiki", "mwezi", "gani", "ngapi", "nini", "je", "nime", "zangu", "yangu", "biashara")
    return sum(1 for w in sw if re.search(rf"\b{w}", q)) >= 1


def answer_question(db: Session, business: Business, question: str, can_see_costs: bool = True) -> dict:
    q = " ".join(question.lower().split())
    bid, tz = business.id, tz_for(business)
    today = local_today(business)
    sw = is_swahili(q)
    period = detect_period(q, today)
    intent = next((name for name, words in INTENTS if any(re.search(rf"\b{re.escape(w)}", q) for w in words)), None)
    if intent in ("profit", "expenses", "value", "dead") and not can_see_costs:
        intent = "restricted"
    data: dict = {}

    if intent == "restock":
        plan = restock_plan(db, bid)
        data = {"items": plan[:12]}
        if not plan:
            answer = ("Hakuna bidhaa inayohitaji kuagizwa sasa hivi." if sw else
                      "Nothing needs reordering right now — all products are above reorder level and not selling fast enough to run out this week.")
        else:
            lines = [f"• {p['name']}: {p['stock_qty']} left" + (f", ~{p['days_left']:g} days of stock" if p["days_left"] is not None else "")
                     + (f" → order {p['suggested_order']}" if p["suggested_order"] else "") for p in plan[:10]]
            total_cost = sum(p["estimated_cost"] for p in plan)
            head = "Bidhaa za kuagiza (kwa mauzo ya wiki 4 zilizopita):" if sw else "Restock plan (based on the last 4 weeks of sales, covering ~2 weeks):"
            answer = head + "\n" + "\n".join(lines)
            if can_see_costs:
                answer += f"\n\n{'Gharama inayokadiriwa' if sw else 'Estimated cost'}: {ksh(total_cost)}"
    elif intent == "debtors":
        owed = debtors(db, bid)
        data = {"items": owed[:12]}
        if not owed:
            answer = "Hakuna mteja anayedaiwa." if sw else "No customer currently owes you money."
        else:
            total = sum(d["balance"] for d in owed)
            lines = [f"• {d['name']}: {ksh(d['balance'])} (oldest {d['oldest_days']} days)" for d in owed[:10]]
            head = f"Wateja {len(owed)} wanadaiwa jumla ya {ksh(total)}:" if sw else f"{len(owed)} customer(s) owe you {ksh(total)} in total:"
            answer = head + "\n" + "\n".join(lines) + ("\n\nTip: open Customers to send a polite WhatsApp reminder." if not sw else "\n\nDokezo: fungua Wateja kutuma ukumbusho kwa WhatsApp.")
    elif intent == "dead":
        dead = dead_stock(db, bid)
        data = {"items": dead[:12]}
        if not dead:
            answer = "Every product with stock has sold in the last 30 days."
        else:
            answer = (f"{len(dead)} product(s) have not sold in 30 days, holding {ksh(sum(d['capital_tied'] for d in dead))} of your capital:\n"
                      + "\n".join(f"• {d['name']}: {d['stock_qty']} units ({ksh(d['capital_tied'])})" for d in dead[:10])
                      + "\n\nIdeas: bundle with a fast seller, discount slightly, or stop reordering.")
    elif intent == "top":
        lo, hi = period.utc_bounds(tz)
        rows = top_products(db, bid, lo, hi, 5)
        data = {"items": rows, "period": period.label}
        if not rows:
            answer = f"No sales recorded for {period.label}."
        else:
            answer = (f"Bidhaa zinazouzika zaidi ({period.label}):" if sw else f"Top sellers ({period.label}):") + "\n" + "\n".join(
                f"{i}. {r['name']} — {r['quantity']} units, {ksh(r['revenue'])}" + (f", profit {ksh(r['gross_profit'])}" if can_see_costs else "")
                for i, r in enumerate(rows, 1))
    elif intent == "expenses":
        r = summary_report(db, business, period)
        data = r["expenses_by_category"]
        if not r["expenses_by_category"]:
            answer = f"No expenses recorded for {period.label}."
        else:
            answer = (f"Matumizi ({period.label}): {ksh(r['operating_expenses'])}" if sw else f"Expenses for {period.label}: {ksh(r['operating_expenses'])}") + "\n" + "\n".join(
                f"• {c['category']}: {ksh(c['amount'])}" for c in r["expenses_by_category"])
    elif intent == "profit":
        r = summary_report(db, business, period)
        data = {k: r[k] for k in ("revenue", "cost_of_goods_sold", "gross_profit", "operating_expenses", "estimated_net_operating_profit")}
        ch = r["change"]["net_profit"]
        trend = "" if ch is None else f" ({'+' if ch >= 0 else ''}{ch}% vs previous {period.days} day(s))"
        if sw:
            answer = (f"Faida ({period.label}):\n• Mauzo: {ksh(r['revenue'])}\n• Gharama ya bidhaa: {ksh(r['cost_of_goods_sold'])}\n"
                      f"• Faida ghafi: {ksh(r['gross_profit'])} ({r['gross_margin_pct']}%)\n• Matumizi: {ksh(r['operating_expenses'])}\n"
                      f"• Faida halisi (makadirio): {ksh(r['estimated_net_operating_profit'])}{trend}")
        else:
            answer = (f"Profit for {period.label}:\n• Revenue: {ksh(r['revenue'])}\n• Cost of goods sold: {ksh(r['cost_of_goods_sold'])}\n"
                      f"• Gross profit: {ksh(r['gross_profit'])} ({r['gross_margin_pct']}% margin)\n• Operating expenses: {ksh(r['operating_expenses'])}\n"
                      f"• Estimated operating profit: {ksh(r['estimated_net_operating_profit'])}{trend}\n\nOnly costs recorded in Grovia are included.")
    elif intent == "sales":
        r = summary_report(db, business, period)
        data = {k: r[k] for k in ("revenue", "transactions", "average_sale")}
        ch = r["change"]["revenue"]
        trend = "" if ch is None else f" That is {'up' if ch >= 0 else 'down'} {abs(ch)}% on the previous {period.days} day(s)."
        if sw:
            answer = f"Mauzo ({period.label}): {ksh(r['revenue'])} katika mauzo {r['transactions']}. Wastani kwa mteja: {ksh(r['average_sale'])}."
        else:
            answer = f"Sales for {period.label}: {ksh(r['revenue'])} across {r['transactions']} transaction(s), averaging {ksh(r['average_sale'])} per sale.{trend}"
    elif intent == "value":
        v = inventory_value(db, bid)
        data = v
        answer = (f"You hold {v['units']:,} units. At cost that is {ksh(v['cost_value'])}; at selling price {ksh(v['retail_value'])} "
                  f"— {ksh(v['potential_profit'])} potential gross profit if it all sells.")
    elif intent == "insights":
        items = [i for i in insights(db, business) if can_see_costs or i["page"] != "reports"]
        data = {"items": items}
        answer = ("Muhtasari wa biashara yako:\n" if sw else "Here's what stands out right now:\n") + "\n".join(
            f"• {i['title']} — {i['detail']}" for i in items[:6])
    elif intent == "restricted":
        answer = "Cost and profit figures are only available to owners and managers."
    else:
        answer = ("Naweza kukusaidia na: mauzo, faida, bidhaa zinazouzika, bidhaa za kuagiza, wateja wanaodaiwa na matumizi. "
                  "Mfano: “Faida yangu wiki hii?”" if sw else
                  "I can answer questions about sales, profit, top sellers, what to restock, who owes you, slow-moving stock, "
                  "expenses and stock value. Try “What did I sell today?”, “What should I restock?” or “Who owes me money?”.")
    return {"answer": answer, "intent": intent or "help", "period": period.label, "data": data,
            "source": "Grovia business records", "as_of": utcnow().isoformat()}
