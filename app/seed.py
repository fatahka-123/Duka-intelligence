"""Create a demo shop with ~60 days of realistic history.

    python -m app.seed                # demo@duka.app / demo-password-123
    python -m app.seed --email me@x.com --password something-long

For demos and development only — never run against a production database.
"""
from __future__ import annotations

import argparse
import random
import secrets
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from .config import IS_SQLITE
from .db import (
    Base, Business, Customer, Expense, MpesaTransaction, Payment, Product, Sale, SaleItem, SessionLocal, Shift, StockMovement,
    Supplier, User, engine, utcnow,
)
from .security import hash_password

CATALOGUE = [  # name, category, cost, price, opening stock, reorder, daily demand
    ("Jogoo Maize Flour 2kg", "Flour", 155, 185, 60, 15, 6), ("Exe Wheat Flour 2kg", "Flour", 165, 195, 40, 10, 3),
    ("Mumias Sugar 1kg", "Sugar", 160, 190, 45, 12, 5), ("Fresh Fri Cooking Oil 1L", "Cooking oil", 290, 340, 30, 8, 2.5),
    ("Brookside Milk 500ml", "Dairy", 55, 65, 40, 20, 9), ("Supa Loaf Bread 400g", "Bakery", 58, 70, 30, 15, 8),
    ("Royco Mchuzi Mix 75g", "Spices", 32, 45, 50, 10, 2), ("Kimbo 1kg", "Cooking oil", 380, 440, 15, 5, 0.8),
    ("Omo Washing Powder 1kg", "Household", 260, 310, 20, 6, 1.2), ("Geisha Soap 225g", "Household", 75, 95, 40, 10, 2),
    ("Safaricom Airtime 100", "Airtime", 95, 100, 200, 30, 7), ("Eggs (tray of 30)", "Dairy", 390, 450, 12, 4, 0.9),
    ("Ketepa Tea Leaves 250g", "Beverages", 135, 165, 25, 6, 1.1), ("Coca-Cola 500ml", "Beverages", 55, 70, 48, 24, 6),
    ("Dasani Water 1L", "Beverages", 45, 60, 36, 12, 3), ("Pishori Rice 1kg", "Rice", 210, 250, 30, 8, 2),
    ("Matchbox (10 pack)", "Household", 40, 50, 20, 5, 0.6), ("Candles (6 pack)", "Household", 95, 120, 12, 4, 0),
    ("Toothpaste Colgate 70g", "Personal care", 105, 130, 4, 6, 0.7), ("Unga wa Ngano Ajab 1kg", "Flour", 85, 82, 20, 5, 0.8),
]
CUSTOMERS = [("Mama Wanjiku", "0712345678", 2000), ("Otieno Hardware", "0722111222", 5000), ("Baraka Kiosk", "0733444555", 3000),
             ("Mzee Kamau", "0700987654", 1500), ("Achieng' Salon", "0745678901", 0)]


def seed(email: str, password: str, days: int = 60) -> None:
    if IS_SQLITE:
        Base.metadata.create_all(engine)
    rnd = random.Random(42)
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.email == email)):
            print(f"{email} already exists — nothing to do.")
            return
        start = utcnow() - timedelta(days=days)
        b = Business(name="Umoja Mini Mart", phone="0711 000 111", address="Moi Avenue, Nairobi", receipt_footer="Asante kwa kununua! Karibu tena.",
                     created_at=start, county="Nairobi", owner_phone="0711000111", daily_summary=True, summary_hour=20, debt_reminders=True,
                     benchmark_opt_in=True, mpesa_mode="simulator", mpesa_shortcode="5123456", mpesa_type="till",
                     mpesa_callback_token=secrets.token_urlsafe(24))
        db.add(b); db.flush()
        owner = User(business_id=b.id, name="Grace Wambui", email=email, password_hash=hash_password(password), role="owner", created_at=start)
        cashier = User(business_id=b.id, name="Brian Kiprop", email=f"cashier.{email}", password_hash=hash_password(password), role="cashier", created_at=start)
        db.add_all([owner, cashier]); db.flush()
        sup = Supplier(business_id=b.id, name="Bidco Distributors", phone="0720 555 000", notes="Delivers Tuesdays", created_at=start)
        db.add(sup)
        customers = [Customer(business_id=b.id, name=n, phone=p, credit_limit=Decimal(l), created_at=start) for n, p, l in CUSTOMERS]
        db.add_all(customers); db.flush()

        products = []
        for i, (name, cat, cost, price, stock, reorder, _) in enumerate(CATALOGUE):
            p = Product(business_id=b.id, name=name, category=cat, cost_price=Decimal(cost), selling_price=Decimal(price),
                        stock_qty=stock * 3, reorder_level=reorder, sku=f"UM{i + 1:03d}", barcode=f"61611000{i + 1:05d}", created_at=start)
            db.add(p); db.flush()
            db.add(StockMovement(business_id=b.id, product_id=p.id, user_id=owner.id, movement_type="opening", quantity=p.stock_qty,
                                 unit_cost=p.cost_price, balance_after=p.stock_qty, note="Opening stock", created_at=start))
            products.append(p)

        for d in range(days):
            day = start + timedelta(days=d + 1)
            if day.replace(hour=4) > utcnow():
                break
            weekday = day.weekday()
            boost = 1.35 if weekday == 5 else 1.15 if weekday == 4 else 0.8 if weekday == 6 else 1.0
            growth = 0.85 + 0.3 * d / days
            # Tuesday delivery restocks fast movers
            if weekday == 1:
                for p, row in zip(products, CATALOGUE):
                    if row[6] and p.stock_qty < row[6] * 10 and p.name != "Toothpaste Colgate 70g":
                        q = int(row[6] * 10)
                        p.stock_qty += q
                        db.add(StockMovement(business_id=b.id, product_id=p.id, user_id=owner.id, supplier_id=sup.id, movement_type="purchase",
                                             quantity=q, unit_cost=p.cost_price, balance_after=p.stock_qty, note="Weekly delivery",
                                             created_at=day.replace(hour=5)))
            for _ in range(int(rnd.gauss(22, 4) * boost * growth)):
                t = day.replace(hour=rnd.choice([4, 5, 6, 9, 10, 13, 14, 15, 16, 16, 17]), minute=rnd.randint(0, 59))
                if t > utcnow():
                    continue
                lines = {}
                for p, row in zip(products, CATALOGUE):
                    if row[6] and rnd.random() < row[6] / 30 and p.stock_qty > 0:
                        lines[p] = min(p.stock_qty, rnd.choice([1, 1, 1, 2, 2, 3]))
                if not lines:
                    continue
                subtotal = sum((p.selling_price * q for p, q in lines.items()), Decimal(0))
                credit = rnd.random() < 0.07
                cust = rnd.choice(customers[:4]) if credit else (rnd.choice(customers) if rnd.random() < 0.1 else None)
                method = "credit" if credit else rnd.choices(["mpesa", "cash", "bank"], [62, 35, 3])[0]
                paid = Decimal(0) if credit else subtotal
                if credit and d < days - 10 and rnd.random() < 0.6:
                    paid = subtotal  # most older credit was repaid
                s = Sale(business_id=b.id, customer_id=cust.id if cust else None, user_id=rnd.choice([owner.id, cashier.id]),
                         reference="DK-" + secrets.token_hex(4).upper(), subtotal=subtotal, discount=0, total=subtotal, amount_paid=paid,
                         payment_method=method, status="completed", created_at=t)
                db.add(s); db.flush()
                for p, q in lines.items():
                    p.stock_qty -= q
                    db.add(SaleItem(sale_id=s.id, product_id=p.id, product_name=p.name, quantity=q, unit_price=p.selling_price,
                                    unit_cost=p.cost_price, line_total=p.selling_price * q))
                    db.add(StockMovement(business_id=b.id, product_id=p.id, user_id=s.user_id, movement_type="sale", quantity=-q,
                                         unit_cost=p.cost_price, balance_after=p.stock_qty, note=s.reference, created_at=t))
                if paid:
                    pm = "mpesa" if method == "credit" else method
                    code = ("S" + secrets.token_hex(5).upper()[:9]) if pm == "mpesa" else ""
                    at = min(utcnow(), t + (timedelta(days=rnd.randint(2, 9)) if credit else timedelta()))
                    db.add(Payment(business_id=b.id, sale_id=s.id, user_id=s.user_id, amount=paid, method=pm, external_ref=code, created_at=at))
                    if code:
                        db.add(MpesaTransaction(business_id=b.id, source="c2b", trans_id=code, amount=paid, phone="2547" + str(rnd.randint(10000000, 99999999))[:8],
                                                payer_name=rnd.choice(["JOHN", "MARY", "PETER", "FAITH", "BRIAN", "AISHA", "KEVIN", "MERCY"]),
                                                status="matched", sale_id=s.id, customer_id=s.customer_id,
                                                matched_by="auto_phone" if credit else rnd.choice(["cashier", "cashier", "stk"]), created_at=at))
            if day.day == 1:
                db.add(Expense(business_id=b.id, user_id=owner.id, category="Rent", description="Shop rent", amount=Decimal(9000), created_at=day.replace(hour=7)))
            if weekday == 5:
                db.add(Expense(business_id=b.id, user_id=owner.id, category="Wages", description="Weekly wages", amount=Decimal(1800), created_at=day.replace(hour=15)))
            if d % 10 == 3:
                db.add(Expense(business_id=b.id, user_id=owner.id, category="Electricity", description="KPLC tokens", amount=Decimal(900), created_at=day.replace(hour=8)))
            if rnd.random() < 0.25:
                db.add(Expense(business_id=b.id, user_id=owner.id, category="Transport", description="Boda to market", amount=Decimal(rnd.choice([200, 300, 400])), created_at=day.replace(hour=6)))
        db.flush()
        _extras(db, rnd, b, owner, cashier, customers, products, start)
        db.commit()
        from .automation import run_business_jobs
        b.summary_hour = 0
        run_business_jobs(db, b)
        b.summary_hour = 20
        db.commit()
    print(f"Demo shop ready. Owner: {email} / {password}   Cashier: cashier.{email} / {password}")


def _extras(db, rnd, b, owner, cashier, customers, products, start):
    """Data that shows off v0.3: shifts with a shortage pattern, fake M-Pesa codes, unmatched payments,
    voids, neighbouring shops for market prices."""
    now = utcnow()
    # Shifts for the last 14 days: owner balances, cashier is short three times
    shortages = {3: -450, 7: -300, 11: -600}
    for d in range(14, 0, -1):
        for u, var in ((owner, 0), (cashier, shortages.get(d, rnd.choice([0, 0, 10, -20])))):
            opened = (now - timedelta(days=d)).replace(hour=4, minute=30)
            closed = opened.replace(hour=16)
            cash = Decimal(sum(float(p.amount) for p in db.scalars(select(Payment).where(
                Payment.business_id == b.id, Payment.user_id == u.id, Payment.method == "cash",
                Payment.created_at >= opened, Payment.created_at <= closed))))
            expected = Decimal(2000) + cash
            db.add(Shift(business_id=b.id, user_id=u.id, opened_at=opened, closed_at=closed, opening_float=Decimal(2000),
                         expected_cash=expected, counted_cash=expected + var, variance=Decimal(var), status="closed",
                         mpesa_total=Decimal(0), sales_count=0))
    # Two M-Pesa codes typed by the cashier that M-Pesa never confirmed (the classic fake-SMS scam)
    for i, s in enumerate(db.scalars(select(Sale).where(Sale.business_id == b.id, Sale.user_id == cashier.id, Sale.payment_method == "mpesa",
                                                        Sale.created_at < now - timedelta(hours=2)).order_by(Sale.created_at.desc()).limit(2))):
        p = db.scalar(select(Payment).where(Payment.sale_id == s.id))
        tx = db.scalar(select(MpesaTransaction).where(MpesaTransaction.sale_id == s.id))
        if p and tx:
            db.delete(tx)
            p.external_ref = ["SFK7Q2M1XZ", "SGH3P9L0QA"][i]
            p.created_at = now - timedelta(days=i + 1)
    # A few voids by the cashier (stock returned properly)
    for s in db.scalars(select(Sale).where(Sale.business_id == b.id, Sale.user_id == cashier.id, Sale.status == "completed",
                                           Sale.created_at > now - timedelta(days=20)).limit(4)):
        for it in db.scalars(select(SaleItem).where(SaleItem.sale_id == s.id)):
            p = db.get(Product, it.product_id)
            p.stock_qty += it.quantity
            db.add(StockMovement(business_id=b.id, product_id=p.id, user_id=cashier.id, movement_type="void", quantity=it.quantity,
                                 unit_cost=it.unit_cost, balance_after=p.stock_qty, note=f"Void {s.reference}", created_at=s.created_at + timedelta(minutes=3)))
        s.status, s.void_reason, s.voided_at, s.voided_by = "voided", "Customer changed mind", s.created_at + timedelta(minutes=3), owner.id
    # A stock count that found missing items
    p = products[1]
    p.stock_qty -= 3
    db.add(StockMovement(business_id=b.id, product_id=p.id, user_id=cashier.id, movement_type="adjustment", quantity=-3, unit_cost=p.cost_price,
                         balance_after=p.stock_qty, note="theft: missing at count", created_at=now - timedelta(days=4)))
    # Recent payments to the till not yet linked to anything
    for amt, name in ((350, "WANJIRU"), (1200, "OCHIENG")):
        db.add(MpesaTransaction(business_id=b.id, source="c2b", trans_id="S" + secrets.token_hex(5).upper()[:9], amount=Decimal(amt),
                                phone="254722***" + str(rnd.randint(100, 999)), payer_name=name, status="unmatched",
                                created_at=now - timedelta(minutes=rnd.randint(5, 50))))
    # Neighbouring shops in the anonymous price network (same barcodes, slightly different prices)
    for n, shift in (("Baraka Shop", -5), ("Mama Mboga Stores", 0), ("Kilimani Kiosk", 5), ("Jirani Mart", -10)):
        nb = Business(name=n, county="Nairobi", benchmark_opt_in=True, daily_summary=False, created_at=start)
        db.add(nb)
        db.flush()
        for i, (name, cat, cost, price, *_rest) in enumerate(CATALOGUE[:14]):
            db.add(Product(business_id=nb.id, name=name, category=cat, cost_price=Decimal(cost), barcode=f"61611000{i + 1:05d}",
                           selling_price=Decimal(price + shift + rnd.choice([-5, 0, 0, 5])), stock_qty=20, created_at=start))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", default="demo@duka.app")
    ap.add_argument("--password", default="demo-password-123")
    ap.add_argument("--days", type=int, default=120)
    a = ap.parse_args()
    seed(a.email.lower(), a.password, a.days)
