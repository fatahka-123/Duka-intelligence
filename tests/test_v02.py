"""Tests for v0.2 fixes and features."""
import uuid

from conftest import PASSWORD, add_product, new_client, register


# ---------------------------------------------------------------- bug fixes
def test_duplicate_lines_cannot_oversell(client, owner):
    p = add_product(client, stock_qty=3)
    r = client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 3}, {"product_id": p["id"], "quantity": 3}]})
    assert r.status_code == 400 and "Insufficient stock" in r.json()["detail"]
    assert client.get("/api/products").json()[0]["stock_qty"] == 3


def test_duplicate_lines_are_merged(client, owner):
    p = add_product(client, stock_qty=5)
    r = client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 1}, {"product_id": p["id"], "quantity": 2}]})
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1 and r.json()["items"][0]["quantity"] == 3


def test_product_edit_does_not_change_stock(client, owner):
    p = add_product(client, stock_qty=4)
    r = client.put(f"/api/products/{p['id']}", json={"name": "Renamed", "cost_price": 100, "selling_price": 160, "stock_qty": 999})
    assert r.status_code == 200 and r.json()["stock_qty"] == 4 and r.json()["name"] == "Renamed"


def test_invalid_payment_method_rejected(client, owner):
    p = add_product(client)
    r = client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 1}], "payment_method": "<script>"})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)  # friendly message, not a list


def test_credit_sale_requires_customer(client, owner):
    p = add_product(client)
    r = client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 1}], "payment_method": "credit"})
    assert r.status_code == 400 and "customer" in r.json()["detail"].lower()


def test_cross_origin_post_blocked(client, owner):
    r = client.post("/api/expenses", json={"category": "Rent", "amount": 10}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


# ---------------------------------------------------------------- features
def test_idempotent_sale(client, owner):
    p = add_product(client, stock_qty=10)
    body = {"items": [{"product_id": p["id"], "quantity": 2}], "client_ref": "till1-abc123"}
    a = client.post("/api/sales", json=body).json()
    b = client.post("/api/sales", json=body).json()
    assert a["id"] == b["id"] and b.get("duplicate") is True
    assert client.get("/api/products").json()[0]["stock_qty"] == 8


def test_void_restores_stock_and_excludes_from_reports(client, owner):
    p = add_product(client, stock_qty=10)
    s = client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 4}]}).json()
    r = client.post(f"/api/sales/{s['id']}/void", json={"reason": "Entered wrong item"})
    assert r.status_code == 200 and r.json()["status"] == "voided" and r.json()["refund_due"] == 600
    assert client.get("/api/products").json()[0]["stock_qty"] == 10
    assert client.get("/api/reports/summary?period=today").json()["revenue"] == 0
    assert client.post(f"/api/sales/{s['id']}/void", json={"reason": "again"}).status_code == 400
    types = [m["type"] for m in client.get(f"/api/products/{p['id']}/movements").json()]
    assert types == ["void", "sale", "opening"]


def test_receive_stock_weighted_average_cost(client, owner):
    p = add_product(client, cost_price=100, stock_qty=10)
    sup = client.post("/api/suppliers", json={"name": "Bidco Distributor"}).json()
    r = client.post(f"/api/products/{p['id']}/stock", json={"quantity": 10, "unit_cost": 120, "supplier_id": sup["id"]})
    assert r.json()["stock_qty"] == 20 and r.json()["cost_price"] == 110
    assert client.get(f"/api/suppliers/{sup['id']}/purchases").json()[0]["total"] == 1200


def test_stock_count_adjustment(client, owner):
    p = add_product(client, stock_qty=10)
    r = client.post(f"/api/products/{p['id']}/adjust", json={"counted_qty": 7, "reason": "count", "note": "Monthly count"})
    assert r.json()["stock_qty"] == 7
    r = client.post(f"/api/products/{p['id']}/adjust", json={"change": -2, "reason": "damaged"})
    assert r.json()["stock_qty"] == 5
    assert client.post(f"/api/products/{p['id']}/adjust", json={"change": -50, "reason": "theft"}).status_code == 400
    assert client.post(f"/api/products/{p['id']}/adjust", json={"reason": "count"}).status_code == 400


def test_barcode_lookup_and_unique_codes(client, owner):
    add_product(client, barcode="6161100100011", sku="SUG1")
    assert client.get("/api/products/lookup?code=6161100100011").json()["sku"] == "SUG1"
    assert client.get("/api/products/lookup?code=SUG1").status_code == 200
    assert client.get("/api/products/lookup?code=nope").status_code == 404
    r = client.post("/api/products", json={"name": "Other", "cost_price": 1, "selling_price": 2, "barcode": "6161100100011"})
    assert r.status_code == 409


def test_credit_limit_and_customer_statement(client, owner):
    p = add_product(client, selling_price=100, cost_price=50, stock_qty=50)
    c = client.post("/api/customers", json={"name": "Otieno", "phone": "0722000000", "credit_limit": 250}).json()
    ok = client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 2}], "customer_id": c["id"], "payment_method": "credit"})
    assert ok.status_code == 200 and ok.json()["paid"] == 0 and ok.json()["payment_status"] == "unpaid"
    over = client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 1}], "customer_id": c["id"], "payment_method": "credit"})
    assert over.status_code == 400 and "credit limit" in over.json()["detail"]
    st = client.get(f"/api/customers/{c['id']}/statement").json()
    assert st["customer"]["balance"] == 200 and st["visits"] == 1
    assert client.get("/api/customers").json()[0]["balance"] == 200
    assert client.get("/api/debtors").json()[0]["balance"] == 200


def test_summary_report_breakdowns(client, owner):
    a = add_product(client, name="Sugar 1kg", cost_price=150, selling_price=180, stock_qty=20)
    b = add_product(client, name="Bread", cost_price=50, selling_price=65, stock_qty=20)
    client.post("/api/sales", json={"items": [{"product_id": a["id"], "quantity": 2}, {"product_id": b["id"], "quantity": 1}], "payment_method": "mpesa"})
    client.post("/api/sales", json={"items": [{"product_id": b["id"], "quantity": 2}], "discount": 10})
    client.post("/api/expenses", json={"category": "Rent", "amount": 100})
    r = client.get("/api/reports/summary?period=7d").json()
    assert r["revenue"] == 360 + 65 + 130 - 10
    assert r["cost_of_goods_sold"] == 300 + 150
    assert r["operating_expenses"] == 100
    assert r["estimated_net_operating_profit"] == 545 - 450 - 100
    assert {m["method"]: m["amount"] for m in r["payment_methods"]} == {"mpesa": 425, "cash": 120}
    assert r["top_products"][0]["name"] == "Sugar 1kg"
    assert len(r["daily"]) == 7 and sum(d["revenue"] for d in r["daily"]) == 545


def test_insights_and_restock(client, owner):
    p = add_product(client, name="Milk 500ml", cost_price=50, selling_price=45, stock_qty=3, reorder_level=5)
    ins = client.get("/api/insights").json()
    titles = " ".join(i["title"] for i in ins)
    assert "below cost" in titles
    plan = client.get("/api/products/summary").json()["restock"]
    assert plan[0]["product_id"] == p["id"] and plan[0]["suggested_order"] > 0


def test_assistant_english_and_swahili(client, owner):
    p = add_product(client, name="Unga", stock_qty=10)
    client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 2}]})
    en = client.post("/api/assistant", json={"question": "What did I sell today?"}).json()
    assert en["intent"] == "sales" and en["period"] == "today" and "300.00" in en["answer"]
    sw = client.post("/api/assistant", json={"question": "Faida yangu wiki hii?"}).json()
    assert sw["intent"] == "profit" and sw["answer"].startswith("Faida")
    assert client.post("/api/assistant", json={"question": "Who owes me money?"}).json()["intent"] == "debtors"
    assert client.post("/api/assistant", json={"question": "What should I restock?"}).json()["intent"] == "restock"
    assert client.post("/api/assistant", json={"question": "hello"}).json()["intent"] == "help"


def test_csv_export_escapes_formulas(client, owner):
    add_product(client, name="=HYPERLINK(\"http://x\")")
    r = client.get("/api/export/products.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "'=HYPERLINK" in r.text


def test_staff_roles_and_permissions(client, owner):
    p = add_product(client, stock_qty=10)
    email = f"cashier-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/staff", json={"name": "Wanjiru", "email": email, "password": PASSWORD, "role": "cashier"})
    assert r.status_code == 200
    staff_id = r.json()["id"]

    cashier = new_client()
    assert cashier.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
    products = cashier.get("/api/products").json()
    assert "cost_price" not in products[0]  # cashiers do not see costs
    assert cashier.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 1}]}).status_code == 200
    for path in ("/api/reports/summary", "/api/insights", "/api/staff", "/api/expenses", "/api/export/sales.csv"):
        assert cashier.get(path).status_code == 403, path
    assert cashier.post("/api/products", json={"name": "x", "cost_price": 1, "selling_price": 2}).status_code == 403
    dash = cashier.get("/api/dashboard").json()
    assert "today_gross_profit" not in dash and dash["today_sales"] == 1

    # Deactivation revokes the live session immediately
    client.patch(f"/api/staff/{staff_id}", json={"active": False})
    assert cashier.get("/api/auth/me").status_code == 401


def test_login_throttling(client):
    acct = register(client)
    other = new_client()
    for _ in range(5):
        assert other.post("/api/auth/login", json={"email": acct["email"], "password": "wrong-password"}).status_code == 401
    r = other.post("/api/auth/login", json={"email": acct["email"], "password": PASSWORD})
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_logout_all_and_password_change(client, owner):
    second = new_client()
    second.post("/api/auth/login", json={"email": owner["email"], "password": PASSWORD})
    assert second.get("/api/auth/me").status_code == 200
    r = client.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "brand-new-password-1"})
    assert r.status_code == 200
    assert second.get("/api/auth/me").status_code == 401  # other device signed out
    assert client.get("/api/auth/me").status_code == 200  # this device keeps working


def test_legacy_v01_password_hash_still_works(client):
    import base64, hashlib, secrets
    from app.db import Business, SessionLocal, User
    salt = secrets.token_bytes(16)
    legacy = base64.urlsafe_b64encode(salt).decode() + "." + base64.urlsafe_b64encode(
        hashlib.pbkdf2_hmac("sha256", b"legacy-password-123", salt, 240000)).decode()
    email = f"legacy-{uuid.uuid4().hex[:8]}@example.com"
    with SessionLocal() as db:
        b = Business(name="Legacy")
        db.add(b); db.flush()
        db.add(User(business_id=b.id, name="Old", email=email, password_hash=legacy)); db.commit()
    assert client.post("/api/auth/login", json={"email": email, "password": "legacy-password-123"}).status_code == 200
    with SessionLocal() as db:
        from sqlalchemy import select
        assert db.scalar(select(User.password_hash).where(User.email == email)).startswith("pbkdf2$600000$")


def test_timezone_today_boundary(client, owner):
    """A sale at 01:00 Nairobi time (22:00 UTC previous day) must count as 'today' locally."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from app.db import Sale, SessionLocal
    p = add_product(client)
    s = client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 1}]}).json()
    nairobi_today_1am = datetime.now(ZoneInfo("Africa/Nairobi")).replace(hour=1, minute=0, second=0, microsecond=0)
    utc_naive = nairobi_today_1am.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    with SessionLocal() as db:
        db.get(Sale, s["id"]).created_at = utc_naive
        db.commit()
    assert client.get("/api/dashboard").json()["today_revenue"] == 150
