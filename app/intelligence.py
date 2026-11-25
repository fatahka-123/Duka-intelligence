"""Higher-level intelligence: Grovia Score, loss prevention, market benchmarks, price coach, quick entry."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import statistics
import time
from collections import defaultdict
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from difflib import SequenceMatcher

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import analytics
from .config import SECRET_KEY
from .db import (
    Business, Customer, Expense, MpesaTransaction, Payment, Product, Sale, SaleItem, Shift, StockMovement, User, utcnow,
)

D0 = Decimal(0)


def clamp(v: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, v))


def interp(x: float, x0: float, y0: float, x1: float, y1: float) -> float:
    if x1 == x0:
        return y1
    return clamp(y0 + (x - x0) * (y1 - y0) / (x1 - x0), min(y0, y1), max(y0, y1))


# ======================================================================= Grovia Score
def monthly_statement(db: Session, b: Business, months: int = 6) -> list[dict]:
    tz = analytics.tz_for(b)
    today = analytics.local_today(b)
    out = []
    first = today.replace(day=1)
    for i in range(months - 1, -1, -1):
        y, m = first.year, first.month - i
        while m <= 0:
            m += 12
            y -= 1
        start = first.replace(year=y, month=m, day=1)
        nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = min(nxt - timedelta(days=1), today)
        lo, hi = analytics.Period(start, end, "").utc_bounds(tz)
        t = analytics.sales_totals(db, b.id, lo, hi)
        exp = analytics.expense_total(db, b.id, lo, hi)
        out.append({"month": start.strftime("%b %Y"), "start": start.isoformat(), "revenue": float(t["revenue"]), "gross_profit": float(t["revenue"] - t["cogs"]),
                    "expenses": float(exp), "net_profit": float(t["revenue"] - t["cogs"] - exp), "transactions": t["count"],
                    "partial": end < nxt - timedelta(days=1)})
    return out


def duka_score(db: Session, b: Business) -> dict:
    """A transparent 0–100 business-health score built only from recorded activity.

    Indicative, not a credit decision: lenders apply their own criteria.
    """
    tz = analytics.tz_for(b)
    today = analytics.local_today(b)
    first_sale = db.scalar(select(func.min(Sale.created_at)).where(Sale.business_id == b.id, Sale.status == "completed"))
    history = (today - analytics.to_local(first_sale, tz).date()).days + 1 if first_sale else 0
    window = max(1, min(90, history))
    p90 = analytics.Period(today - timedelta(days=window - 1), today, "")
    lo, hi = p90.utc_bounds(tz)

    # Consistency: share of days with at least one sale
    trading_days = {analytics.to_local(dt, tz).date() for dt in db.scalars(select(Sale.created_at).where(
        Sale.business_id == b.id, Sale.status == "completed", Sale.created_at >= lo, Sale.created_at < hi))}
    consistency = len(trading_days) / window * 100 if history else 0

    # Growth: last 30 days vs the 30 before
    c30 = analytics.Period(today - timedelta(days=29), today, "")
    l1, h1 = c30.utc_bounds(tz)
    l0, h0 = c30.previous().utc_bounds(tz)
    cur, prev = analytics.sales_totals(db, b.id, l1, h1), analytics.sales_totals(db, b.id, l0, h0)
    g = analytics.pct_change(cur["revenue"], prev["revenue"])
    growth = 50 if g is None else (interp(g, -30, 0, 0, 60) if g < 0 else interp(g, 0, 60, 30, 100))

    # Profitability: net margin over the window
    t = analytics.sales_totals(db, b.id, lo, hi)
    exp = analytics.expense_total(db, b.id, lo, hi)
    net = t["revenue"] - t["cogs"] - exp
    margin = float(net / t["revenue"] * 100) if t["revenue"] else 0
    profitability = 0 if margin <= 0 else (interp(margin, 0, 20, 5, 55) if margin < 5 else interp(margin, 5, 55, 15, 100))

    # Collections: of credit given 14–120 days ago, how much has been repaid?
    c_lo, c_hi = utcnow() - timedelta(days=120), utcnow() - timedelta(days=14)
    credit_sales = list(db.execute(select(Sale.total, Sale.amount_paid, Sale.id).where(
        Sale.business_id == b.id, Sale.status == "completed", Sale.customer_id.is_not(None),
        Sale.created_at >= c_lo, Sale.created_at < c_hi)).all())
    initial_paid = defaultdict(lambda: D0)
    if credit_sales:
        for sid, amt, created, sale_created in db.execute(select(Payment.sale_id, Payment.amount, Payment.created_at, Sale.created_at)
                                                          .join(Sale, Sale.id == Payment.sale_id).where(Sale.id.in_([s.id for s in credit_sales]))):
            if created - sale_created < timedelta(minutes=5):
                initial_paid[sid] += amt
    issued = sum((s.total - initial_paid[s.id] for s in credit_sales), D0)
    repaid = sum((s.amount_paid - initial_paid[s.id] for s in credit_sales), D0)
    collections = 80.0 if issued <= 0 else clamp(float(repaid / issued) * 100)
    collection_rate = None if issued <= 0 else round(float(repaid / issued) * 100, 1)

    # Record keeping: expenses logged regularly, stock counted, M-Pesa reconciled
    since28 = utcnow() - timedelta(days=28)
    exp_weeks = {analytics.to_local(dt, tz).isocalendar()[1] for dt in db.scalars(select(Expense.created_at).where(
        Expense.business_id == b.id, Expense.created_at >= since28))}
    counted = db.scalar(select(func.count(StockMovement.id)).where(StockMovement.business_id == b.id, StockMovement.movement_type == "adjustment",
                                                                   StockMovement.created_at >= utcnow() - timedelta(days=45)))
    shifts = db.scalar(select(func.count(Shift.id)).where(Shift.business_id == b.id, Shift.status == "closed", Shift.closed_at >= since28))
    records = min(len(exp_weeks), 4) / 4 * 40 + (20 if counted else 0) + (20 if b.mpesa_mode in ("sandbox", "production") else 0) + (20 if shifts else 0)

    tenure = clamp(history / 180 * 100)
    parts = {
        "consistency": {"label": "Trading consistency", "score": round(consistency), "weight": 25,
                        "detail": f"Sold on {len(trading_days)} of the last {window} days"},
        "profitability": {"label": "Profitability", "score": round(profitability), "weight": 20,
                          "detail": f"Net margin {margin:.1f}% after recorded expenses"},
        "collections": {"label": "Credit collection", "score": round(collections), "weight": 20,
                        "detail": "No customer credit given" if collection_rate is None else f"{collection_rate}% of credit repaid"},
        "growth": {"label": "Growth", "score": round(growth), "weight": 15,
                   "detail": "Not enough history" if g is None else f"Sales {'+' if g >= 0 else ''}{g}% vs previous 30 days"},
        "records": {"label": "Record keeping", "score": round(records), "weight": 10,
                    "detail": f"Expenses logged {min(len(exp_weeks), 4)}/4 weeks · {'stock counted' if counted else 'no stock count'} · "
                              f"{'shift cash-ups' if shifts else 'no cash-ups'} · {'M-Pesa live' if b.mpesa_mode in ('sandbox', 'production') else 'M-Pesa not linked'}"},
        "tenure": {"label": "History on Grovia", "score": round(tenure), "weight": 10, "detail": f"{history} day(s) of records"},
    }
    score = round(sum(p["score"] * p["weight"] for p in parts.values()) / 100)
    grade = "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D"
    months = monthly_statement(db, b, 6)
    if first_sale:
        start_month = analytics.to_local(first_sale, tz).date().replace(day=1)
        started_mid_month = analytics.to_local(first_sale, tz).date().day > 3
        months = [m for m in months if m["start"] >= start_month.isoformat()]
        if started_mid_month and months:
            months[0]["partial"] = True
    else:
        months = []
    full = [m for m in months if not m["partial"]] or [m for m in months if m["revenue"] > 0]
    avg_net = statistics.mean(m["net_profit"] for m in full) if full else 0
    avg_rev = statistics.mean(m["revenue"] for m in full) if full else 0
    capacity = max(0.0, avg_net * 0.3)  # keep repayments under ~30% of monthly profit
    indicative = int(capacity * 3 // 1000 * 1000)
    tips = {
        "consistency": "Record every sale, every day — even slow days count.",
        "profitability": "Fix products priced below cost and record all expenses so profit is accurate.",
        "collections": "Turn on automatic debt reminders and set credit limits.",
        "growth": "Use the restock plan so best-sellers never run out.",
        "records": "Log expenses weekly, do a monthly stock count, close shifts, and link M-Pesa.",
        "tenure": "Keep using Grovia — lenders value at least 6 months of history.",
    }
    weakest = sorted(parts.items(), key=lambda kv: kv[1]["score"])[:3]
    return {
        "score": score, "grade": grade, "ready": history >= 30,
        "status": "Building history — needs 30 days of records" if history < 30 else
                  {"A": "Strong", "B": "Good", "C": "Fair", "D": "Needs work"}[grade],
        "components": parts, "history_days": history,
        "average_monthly_revenue": round(avg_rev, 2), "average_monthly_net_profit": round(avg_net, 2),
        "indicative_loan": indicative if history >= 60 else 0,
        "indicative_repayment": round(capacity, 2),
        "tips": [tips[k] for k, v in weakest if v["score"] < 75],
        "months": months,
        "disclaimer": "Indicative only, calculated from records entered in Grovia. Not a credit decision or financial advice.",
    }


# ------------------------------------------------------------------ lender share links (signed, expiring, revocable)
def _sig(raw: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(SECRET_KEY.encode(), ("share:" + raw).encode(), hashlib.sha256).digest()).decode().rstrip("=")


def share_token(b: Business, days: int = 30) -> str:
    raw = base64.urlsafe_b64encode(json.dumps({"b": b.id, "v": b.share_version, "e": int(time.time()) + days * 86400},
                                              separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{raw}.{_sig(raw)}"


def read_share_token(db: Session, token: str) -> tuple[Business, dict] | None:
    try:
        raw, sig = token.split(".")
        if not hmac.compare_digest(sig, _sig(raw)):
            return None
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if data["e"] < time.time():
            return None
        b = db.get(Business, int(data["b"]))
        if not b or b.share_version != data["v"]:
            return None
        return b, data
    except Exception:
        return None


# ======================================================================= Loss prevention
def loss_watch(db: Session, b: Business, days: int = 30) -> dict:
    since = utcnow() - timedelta(days=days)
    users = {u.id: u for u in db.scalars(select(User).where(User.business_id == b.id))}
    stats = {uid: {"user_id": uid, "name": u.name, "role": u.role, "sales": 0, "revenue": 0.0, "voids": 0, "void_value": 0.0,
                   "discounts": 0.0, "discount_rate": 0.0, "cash_variance": 0.0, "short_shifts": 0, "write_offs": 0.0,
                   "unverified_codes": 0, "flags": [], "risk": 0} for uid, u in users.items()}

    subtotals = defaultdict(float)
    for uid, total, sub, disc in db.execute(select(Sale.user_id, Sale.total, Sale.subtotal, Sale.discount).where(
            Sale.business_id == b.id, Sale.status == "completed", Sale.created_at >= since)):
        if uid in stats:
            s = stats[uid]
            s["sales"] += 1
            s["revenue"] += float(total)
            s["discounts"] += float(disc)
            subtotals[uid] += float(sub)
    for sale in db.scalars(select(Sale).where(Sale.business_id == b.id, Sale.status == "voided", Sale.created_at >= since)):
        if sale.user_id in stats:  # attributed to the cashier who rang it up
            stats[sale.user_id]["voids"] += 1
            stats[sale.user_id]["void_value"] += float(sale.total)
    for sh in db.scalars(select(Shift).where(Shift.business_id == b.id, Shift.status == "closed", Shift.closed_at >= since)):
        if sh.user_id in stats and sh.variance is not None:
            stats[sh.user_id]["cash_variance"] += float(sh.variance)
            if sh.variance < -50:
                stats[sh.user_id]["short_shifts"] += 1
    for m in db.scalars(select(StockMovement).where(StockMovement.business_id == b.id, StockMovement.movement_type == "adjustment",
                                                    StockMovement.quantity < 0, StockMovement.created_at >= since)):
        if m.user_id in stats:
            stats[m.user_id]["write_offs"] += float((m.unit_cost or 0) * -m.quantity)
    from .mpesa import stale_unverified_codes
    for p in stale_unverified_codes(db, b, 15):
        if p.user_id in stats:
            stats[p.user_id]["unverified_codes"] += 1

    team_disc = sum(s["discounts"] for s in stats.values()) / max(sum(subtotals.values()), 1) * 100
    for uid, s in stats.items():
        s["discount_rate"] = round(s["discounts"] / subtotals[uid] * 100, 1) if subtotals[uid] else 0
        risk = 0
        if s["sales"] and s["voids"] / max(s["sales"], 1) > 0.03 and s["voids"] >= 2:
            risk += 25
            s["flags"].append(f"{s['voids']} voids ({s['voids'] / s['sales'] * 100:.0f}% of sales)")
        if s["discount_rate"] > max(3, team_disc * 2) and s["discounts"] > 200:
            risk += 20
            s["flags"].append(f"Discounts {s['discount_rate']}% of sales (team avg {team_disc:.1f}%)")
        if s["short_shifts"]:
            risk += min(35, 15 * s["short_shifts"])
            s["flags"].append(f"Cash short on {s['short_shifts']} shift(s), total KSh {-s['cash_variance']:,.0f}" if s["cash_variance"] < 0
                              else f"Short on {s['short_shifts']} shift(s)")
        if s["write_offs"] > 500:
            risk += 15
            s["flags"].append(f"Wrote off KSh {s['write_offs']:,.0f} of stock")
        if s["unverified_codes"]:
            risk += 30
            s["flags"].append(f"{s['unverified_codes']} M-Pesa code(s) never confirmed by M-Pesa")
        s["risk"] = min(100, risk)
        s["level"] = "high" if risk >= 50 else "medium" if risk >= 25 else "low"
        for k in ("revenue", "discounts", "void_value", "cash_variance", "write_offs"):
            s[k] = round(s[k], 2)

    t = analytics.sales_totals(db, b.id, since, utcnow())
    shrink = sum(float((m.unit_cost or 0) * -m.quantity) for m in db.scalars(select(StockMovement).where(
        StockMovement.business_id == b.id, StockMovement.movement_type == "adjustment", StockMovement.quantity < 0,
        StockMovement.created_at >= since)))
    return {
        "days": days,
        "shrinkage_value": round(shrink, 2),
        "shrinkage_pct": round(shrink / float(t["revenue"]) * 100, 2) if t["revenue"] else 0,
        "staff": sorted([s for s in stats.values() if s["sales"] or s["flags"]], key=lambda s: -s["risk"]),
        "unverified_codes": [{"code": p.external_ref, "amount": float(p.amount), "sale_id": p.sale_id, "at": p.created_at.isoformat(),
                              "user": users[p.user_id].name if p.user_id in users else "—"} for p in stale_unverified_codes(db, b, 15)],
    }


# ======================================================================= Market benchmarks
def _bench_key(p: Product) -> str | None:
    if p.barcode and len(p.barcode) >= 8:
        return "bc:" + p.barcode
    return None


def benchmarks(db: Session, b: Business, min_shops: int = 3) -> list[dict]:
    """Anonymous median selling price among opted-in shops (k-anonymity: at least ``min_shops`` other shops)."""
    if not b.benchmark_opt_in:
        return []
    mine = {k: p for p in db.scalars(select(Product).where(Product.business_id == b.id, Product.active.is_(True))) if (k := _bench_key(p))}
    if not mine:
        return []
    rows = db.execute(select(Product.barcode, Product.selling_price, Business.id, Business.county)
                      .join(Business, Business.id == Product.business_id)
                      .where(Business.benchmark_opt_in.is_(True), Business.id != b.id, Product.active.is_(True),
                             Product.barcode.in_([k[3:] for k in mine]))).all()
    by_code: dict[str, dict] = defaultdict(lambda: {"all": {}, "local": {}})
    for code, price, bid, county in rows:
        by_code[code]["all"][bid] = float(price)
        if b.county and county == b.county:
            by_code[code]["local"][bid] = float(price)
    out = []
    for key, p in mine.items():
        data = by_code.get(key[3:])
        if not data:
            continue
        scope, prices = ("local", data["local"]) if len(data["local"]) >= min_shops else ("national", data["all"])
        if len(prices) < min_shops:
            continue
        med = statistics.median(prices.values())
        mine_price = float(p.selling_price)
        diff = mine_price - med
        out.append({"product_id": p.id, "name": p.name, "your_price": mine_price, "median": round(med, 2), "shops": len(prices),
                    "scope": f"{b.county} shops" if scope == "local" else "Grovia shops nationally",
                    "difference": round(diff, 2), "difference_pct": round(diff / med * 100, 1) if med else 0,
                    "position": "above" if diff > med * 0.03 else "below" if diff < -med * 0.03 else "in line"})
    return sorted(out, key=lambda x: -abs(x["difference_pct"]))


# ======================================================================= Price coach
def round_price(v: Decimal) -> Decimal:
    """Round to shop-friendly prices: nearest 1 under 50, nearest 5 under 1000, nearest 10 above."""
    step = Decimal(1) if v < 50 else Decimal(5) if v < 1000 else Decimal(10)
    return (v / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step


def price_suggestion(old_cost: Decimal, new_cost: Decimal, price: Decimal) -> dict | None:
    if old_cost <= 0 or new_cost <= old_cost * Decimal("1.03") or price <= 0:
        return None
    old_margin = (price - old_cost) / price
    new_margin = (price - new_cost) / price
    target = max(old_margin, Decimal("0.05"))
    suggested = round_price(new_cost / (1 - target)) if target < 1 else price
    if suggested <= price:
        return None
    return {"cost_change_pct": float(round((new_cost - old_cost) / old_cost * 100, 1)),
            "old_margin_pct": float(round(old_margin * 100, 1)), "new_margin_pct": float(round(new_margin * 100, 1)),
            "suggested_price": float(suggested)}


# ======================================================================= Quick entry (natural-language bookkeeping)
ALIASES = {"sukari": "sugar", "maziwa": "milk", "mkate": "bread", "mafuta": "oil", "mchele": "rice", "sabuni": "soap", "chai": "tea",
           "majani": "tea", "maji": "water", "mayai": "eggs", "yai": "eggs", "unga": "flour", "ngano": "wheat", "soda": "cola",
           "sigara": "cigarette", "kiberiti": "matchbox", "mshumaa": "candles", "dawa ya meno": "toothpaste", "nyama": "meat"}
EXPENSE_WORDS = ("expense", "spent", "paid for", "matumizi", "gharama", "nimelipa", "nimetumia", "lipa")
RECEIVE_WORDS = ("received", "receive", "restock", "delivery", "delivered", "nimepokea", "imefika", "stock in", "bought stock", "nimenunua")
PAYMENT_WORDS = ("paid", "amelipa", "amelipia", "has paid", "settled", "repaid")
SALE_WORDS = ("sold", "sell", "sale", "nimeuza", "uza", "mauzo")
EXPENSE_CATEGORIES = [
    (("transport", "usafiri", "boda", "matatu", "fare", "fuel", "petrol"), "Transport"), (("rent", "kodi"), "Rent"),
    (("stima", "electricity", "kplc", "token", "tokens", "umeme"), "Electricity"), (("water", "maji"), "Water"),
    (("wages", "salary", "mshahara", "worker", "kibarua"), "Wages"), (("airtime", "internet", "bundles", "wifi", "data"), "Internet & airtime"),
    (("licence", "license", "permit", "county", "kanjo"), "Licences & permits"), (("repair", "fundi", "repairs"), "Repairs"),
    (("bags", "packaging", "paper bags", "karatasi"), "Packaging"), (("security", "askari", "watchman"), "Security"),
]
STOP = {"and", "na", "the", "of", "pcs", "pieces", "x", "kwa", "to", "for", "a", "an", "&", "on", "via", "by", "with", "pia", "ya", "za", "wa"}


def _words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", s.lower()) if w not in STOP]


def _expand(words: list[str]) -> list[str]:
    out = list(words)
    for w in words:
        if w in ALIASES:
            out += ALIASES[w].split()
    return out


def match_product(text: str, products: list[Product]) -> tuple[Product | None, float, list[Product]]:
    base = _words(text)
    if not base:
        return None, 0, []
    groups = [[w] + (ALIASES[w].split() if w in ALIASES else []) for w in base]  # a word OR its translation counts once
    words = [w for g in groups for w in g]
    scored = []
    for p in products:
        pw = _words(p.name + " " + (p.category or ""))
        hit = lambda w: any(x == w or (len(w) >= 3 and x.startswith(w)) or (len(x) >= 4 and w.startswith(x)) for x in pw)
        overlap = sum(1 for g in groups if any(hit(w) for w in g)) / len(groups)
        fuzzy = SequenceMatcher(None, " ".join(words), p.name.lower()).ratio()
        exact = 0.2 if (p.sku and p.sku.lower() in words) or (p.barcode and p.barcode in words) else 0
        scored.append((overlap * 0.75 + fuzzy * 0.25 + exact, p))
    scored.sort(key=lambda x: -x[0])
    best, p = scored[0]
    close = [q for s, q in scored[1:4] if s >= best - 0.25 and s > 0.3]
    return (p if best >= 0.45 else None), round(best, 2), ([p] + close if close else [])


def _segments(text: str) -> list[tuple[int, str]]:
    """'2 unga 1 sugar' / 'unga 2, sukari 1' / 'two loaves' → [(qty, words)]."""
    parts = [x for x in re.split(r",|;|\band\b|\bna\b|&|\+", text) if x.strip()]
    segs: list[tuple[int, str]] = []
    for part in parts:
        tokens = re.findall(r"\d+|[^\d\s]+", part)
        if sum(t.isdigit() for t in tokens) <= 1:
            nums = [int(t) for t in tokens if t.isdigit()]
            words = " ".join(t for t in tokens if not t.isdigit()).strip()
            if words:
                segs.append((nums[0] if nums else 1, words))
            continue
        number_first = tokens[0].isdigit()
        cur_q, cur_w = None, []
        for t in tokens:
            if t.isdigit():
                if number_first:
                    if cur_w and cur_q is not None:
                        segs.append((cur_q, " ".join(cur_w)))
                    cur_q, cur_w = int(t), []
                else:
                    if cur_w:
                        segs.append((int(t), " ".join(cur_w)))
                    cur_w = []
            else:
                cur_w.append(t)
        if number_first and cur_w and cur_q is not None:
            segs.append((cur_q, " ".join(cur_w)))
    return [(max(1, min(q, 100000)), w) for q, w in segs if _words(w)]


def _find_customer(db: Session, bid: int, text: str) -> Customer | None:
    words = _words(text)
    best, best_c = 0.0, None
    for c in db.scalars(select(Customer).where(Customer.business_id == bid)):
        cw = _words(c.name)
        hits = sum(1 for w in words if len(w) >= 3 and any(x.startswith(w) or w.startswith(x) for x in cw if len(x) >= 3))
        score = hits / max(len(cw), 1)
        if score > best:
            best, best_c = score, c
    return best_c if best >= 0.5 else None


def _method(t: str) -> str:
    if re.search(r"\bm-?pesa\b|\bmpesa\b|\btill\b|\bpaybill\b", t):
        return "mpesa"
    if re.search(r"\bcredit\b|\bdeni\b|\bon account\b|\bmkopo\b|\bkopa\b", t):
        return "credit"
    if re.search(r"\bbank\b", t):
        return "bank"
    return "cash"


def _strip_words(t: str, words) -> str:
    for w in sorted(words, key=len, reverse=True):
        t = re.sub(rf"\b{re.escape(w)}\b", " ", t)
    return t


def parse_quick_entry(db: Session, b: Business, text: str, can_manage: bool) -> dict:
    t = " ".join(text.lower().split())
    has = lambda words: any(re.search(rf"\b{re.escape(w)}", t) for w in words)
    amounts = [int(x.replace(",", "")) for x in re.findall(r"\d[\d,]*", t)]
    products = list(db.scalars(select(Product).where(Product.business_id == b.id, Product.active.is_(True))))

    # Expense: "expense 500 transport", "nimelipa stima 1500"
    if has(EXPENSE_WORDS) and not has(SALE_WORDS) and amounts:
        if not can_manage:
            return {"intent": "error", "message": "Only owners and managers can record expenses."}
        cat = next((c for words, c in EXPENSE_CATEGORIES if any(re.search(rf"\b{w}", t) for w in words)), "Other")
        desc = _strip_words(t, EXPENSE_WORDS + ("ksh", "kes", "shillings", "bob", "sh")).strip()
        desc = re.sub(r"\d[\d,]*", "", desc).strip(" ,.")
        return {"intent": "expense", "expense": {"category": cat, "amount": max(amounts), "description": desc[:240]},
                "summary": f"Record expense: {cat} KSh {max(amounts):,}" + (f" ({desc})" if desc else "")}

    # Customer payment: "Kamau paid 500 mpesa", "Mama Wanjiku amelipa 1000"
    if has(PAYMENT_WORDS) and not has(SALE_WORDS) and not has(EXPENSE_WORDS) and amounts:
        c = _find_customer(db, b.id, _strip_words(t, PAYMENT_WORDS))
        if not c:
            return {"intent": "error", "message": "I couldn't tell which customer paid. Use their saved name, e.g. “Kamau paid 500”."}
        owed = analytics.debtors(db, b.id)
        bal = next((d["balance"] for d in owed if d["customer_id"] == c.id), 0)
        if bal <= 0:
            return {"intent": "error", "message": f"{c.name} has no outstanding balance."}
        amount = min(max(amounts), bal)
        method = _method(t)
        return {"intent": "payment", "payment": {"customer_id": c.id, "customer": c.name, "amount": amount,
                                                  "method": "cash" if method == "credit" else method, "balance": bal},
                "summary": f"{c.name} pays KSh {amount:,.0f} ({'M-Pesa' if method == 'mpesa' else method}) — balance {bal:,.0f} → {bal - amount:,.0f}"}

    # Stock received: "received 24 milk", "nimepokea sukari 50"
    if has(RECEIVE_WORDS) and not has(SALE_WORDS):
        if not can_manage:
            return {"intent": "error", "message": "Only owners and managers can receive stock."}
        lines, unclear = [], []
        for q, words in _segments(_strip_words(t, RECEIVE_WORDS + ("stock", "today", "leo"))):
            p, conf, options = match_product(words, products)
            if p:
                lines.append({"product_id": p.id, "name": p.name, "quantity": q, "unit_cost": float(p.cost_price), "confidence": conf})
            else:
                unclear.append({"text": words, "options": [{"product_id": o.id, "name": o.name} for o in options]})
        if not lines:
            return {"intent": "error", "message": "I couldn't match any products. Try “received 24 milk”.", "unclear": unclear}
        return {"intent": "receive", "lines": lines, "unclear": unclear,
                "summary": "Receive stock: " + ", ".join(f"{l['quantity']} × {l['name']}" for l in lines)}

    # Sale (default): "sold 2 unga 1 sugar mpesa", "nimeuza mkate 3 kwa Kamau deni"
    method = _method(t)
    customer = None
    m = re.search(r"\b(?:to|kwa|for)\s+([a-z' ]+?)(?:\s+(?:on|kwa|via|by|cash|mpesa|m-pesa|credit|deni)\b|$)", t)
    if m:
        customer = _find_customer(db, b.id, m.group(1))
    body = _strip_words(t, SALE_WORDS + ("cash", "mpesa", "m-pesa", "credit", "deni", "on credit", "bank", "today", "leo", "pesa taslimu"))
    if customer and m:
        body = body.replace(m.group(1), " ")
    lines, unclear = [], []
    for q, words in _segments(body):
        p, conf, options = match_product(words, products)
        if p:
            existing = next((l for l in lines if l["product_id"] == p.id), None)
            if existing:
                existing["quantity"] += q
            else:
                lines.append({"product_id": p.id, "name": p.name, "quantity": q, "unit_price": float(p.selling_price),
                              "stock": p.stock_qty, "confidence": conf,
                              "alternatives": [{"product_id": o.id, "name": o.name, "unit_price": float(o.selling_price), "stock": o.stock_qty}
                                               for o in options if o.id != p.id]})
        else:
            unclear.append({"text": words, "options": [{"product_id": o.id, "name": o.name} for o in options]})
    for l in lines:
        l["line_total"] = round(l["unit_price"] * l["quantity"], 2)
        if l["quantity"] > l["stock"]:
            l["warning"] = f"Only {l['stock']} in stock"
    if not lines:
        return {"intent": "error", "unclear": unclear,
                "message": "I couldn't understand that. Examples: “sold 2 unga 1 sugar mpesa”, “expense 500 transport”, "
                           "“received 24 milk”, “Kamau paid 500”."}
    total = sum(l["line_total"] for l in lines)
    needs = []
    if method == "credit" and not customer:
        needs.append("customer")
    return {"intent": "sale", "lines": lines, "unclear": unclear, "payment_method": method,
            "customer_id": customer.id if customer else None, "customer": customer.name if customer else None, "total": total, "needs": needs,
            "summary": "Sell " + ", ".join(f"{l['quantity']} × {l['name']}" for l in lines) + f" = KSh {total:,.0f} · "
                       + {"mpesa": "M-Pesa", "credit": "on credit", "bank": "bank", "cash": "cash"}[method]
                       + (f" · {customer.name}" if customer else "")}
