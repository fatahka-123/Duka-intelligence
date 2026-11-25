"""Tests for v0.3: M-Pesa reconciliation & fraud guards, shifts, automation, Duka Score, loss watch,
benchmarks, price coach and quick entry."""
import uuid
from datetime import timedelta

from conftest import PASSWORD, add_product, new_client, register


def enable_simulator(client):
    r = client.put("/api/mpesa/settings", json={"mode": "simulator", "shortcode": "123456", "type": "till"})
    assert r.status_code == 200, r.text


def backdate_tx(tx_id, seconds=10):
    from app.db import MpesaTransaction, SessionLocal
    with SessionLocal() as db:
        t = db.get(MpesaTransaction, tx_id)
        t.created_at -= timedelta(seconds=seconds)
        db.commit()


# ---------------------------------------------------------------- M-Pesa
def test_reused_mpesa_code_is_blocked_even_without_integration(client, owner):
    p = add_product(client, stock_qty=10)
    body = {"items": [{"product_id": p["id"], "quantity": 1}], "payment_method": "mpesa", "payment_ref": "SKL2AB3CD4"}
    assert client.post("/api/sales", json=body).status_code == 200
    r = client.post("/api/sales", json=body)
    assert r.status_code == 400 and "already used" in r.json()["detail"]
    bad = client.post("/api/sales", json={**body, "payment_ref": "123"})
    assert bad.status_code == 400


def test_stk_push_flow_in_simulator(client, owner):
    enable_simulator(client)
    p = add_product(client, selling_price=150, stock_qty=10)
    tx = client.post("/api/mpesa/stk", json={"phone": "0712345678", "amount": 150}).json()
    assert tx["status"] == "pending"
    assert client.get(f"/api/mpesa/stk/{tx['id']}").json()["status"] == "pending"  # customer hasn't typed PIN yet
    backdate_tx(tx["id"])
    done = client.get(f"/api/mpesa/stk/{tx['id']}").json()
    assert done["status"] == "unmatched" and done["trans_id"]
    # the till completes the sale with the confirmed receipt → verified and linked
    sale = client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 1}], "payment_method": "mpesa",
                                           "payment_ref": done["trans_id"]}).json()
    assert sale["mpesa_verified"] is True
    feed = client.get("/api/mpesa/feed").json()
    assert feed["items"][0]["status"] == "matched" and feed["items"][0]["sale_id"] == sale["id"]


def test_stk_cancelled_by_customer(client, owner):
    enable_simulator(client)
    tx = client.post("/api/mpesa/stk", json={"phone": "0712345000", "amount": 50}).json()  # ...000 simulates a cancel
    backdate_tx(tx["id"])
    assert client.get(f"/api/mpesa/stk/{tx['id']}").json()["status"] == "failed"


def test_unconfirmed_code_rejected_and_short_payment_detected(client, owner):
    enable_simulator(client)
    p = add_product(client, selling_price=500, stock_qty=10)
    paid = client.post("/api/mpesa/simulate", json={"amount": 200}).json()
    r = client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 1}], "payment_method": "mpesa", "payment_ref": paid["trans_id"]})
    assert r.status_code == 400 and "less than" in r.json()["detail"]
    chk = client.get("/api/mpesa/check-code?code=QQQ1234567&amount=10").json()
    assert chk["status"] == "unverified"


def test_c2b_debt_repayment_auto_matched_by_phone(client, owner):
    enable_simulator(client)
    p = add_product(client, selling_price=100, stock_qty=20)
    c = client.post("/api/customers", json={"name": "Mama Wanjiku", "phone": "0711 222 333"}).json()
    for _ in range(2):
        client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 2}], "customer_id": c["id"], "payment_method": "credit"})
    tx = client.post("/api/mpesa/simulate", json={"amount": 300, "phone": "254711222333", "name": "WANJIKU"}).json()
    assert tx["status"] == "matched" and tx["matched_by"] == "auto_phone"
    assert client.get(f"/api/customers/{c['id']}/statement").json()["customer"]["balance"] == 100
    statement = client.get(f"/api/customers/{c['id']}/statement").json()["sales"]
    assert sorted(s["balance"] for s in statement) == [0, 100]  # oldest debt cleared first


def test_c2b_paybill_reference_matches_sale(client, owner):
    enable_simulator(client)
    p = add_product(client, selling_price=100, stock_qty=20)
    c = client.post("/api/customers", json={"name": "Otieno"}).json()
    s = client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 3}], "customer_id": c["id"], "payment_method": "credit"}).json()
    tx = client.post("/api/mpesa/simulate", json={"amount": 300, "bill_ref": s["reference"].lower()}).json()
    assert tx["status"] == "matched" and tx["matched_by"] == "auto_ref"
    assert client.get(f"/api/sales/{s['id']}").json()["balance"] == 0


def test_unmatched_payment_can_be_attached(client, owner):
    enable_simulator(client)
    p = add_product(client, selling_price=100, stock_qty=20)
    c = client.post("/api/customers", json={"name": "Baraka"}).json()
    s = client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 1}], "customer_id": c["id"], "payment_method": "credit"}).json()
    tx = client.post("/api/mpesa/simulate", json={"amount": 100, "phone": "254799000111"}).json()
    assert tx["status"] == "unmatched"
    assert client.get("/api/mpesa/feed?status=unmatched").json()["unmatched_count"] == 1
    r = client.post(f"/api/mpesa/{tx['id']}/attach", json={"customer_id": c["id"]})
    assert r.status_code == 200 and client.get(f"/api/sales/{s['id']}").json()["balance"] == 0
    assert client.post(f"/api/mpesa/{tx['id']}/attach", json={"customer_id": c["id"]}).status_code == 400


def test_daraja_callbacks_are_idempotent_and_token_protected(client, owner):
    from app.db import Business, SessionLocal
    client.put("/api/mpesa/settings", json={"mode": "sandbox", "shortcode": "174379", "type": "paybill",
                                            "consumer_key": "k" * 20, "consumer_secret": "s" * 20, "passkey": "p" * 20})
    settings = client.get("/api/mpesa/settings").json()
    assert settings["consumer_key"].startswith("••") and "kkkk" not in str(settings["consumer_key"])[:6]
    with SessionLocal() as db:
        b = db.get(Business, owner["business"]["id"])
        token = b.mpesa_callback_token
        assert b.mpesa_consumer_key and "kkkk" not in b.mpesa_consumer_key  # encrypted at rest
    payload = {"TransactionType": "Pay Bill", "TransID": "TKA1BC2DE3", "TransTime": "20261125101010", "TransAmount": "250.00",
               "BusinessShortCode": "174379", "BillRefNumber": "", "MSISDN": "254700000001", "FirstName": "JOHN"}
    anon = new_client()
    assert anon.post("/api/mpesa/callback/wrong-token-xxxxxxxxxxxxxxxx/c2b/confirmation", json=payload).status_code == 404
    for _ in range(2):  # Safaricom retries — must be recorded once
        assert anon.post(f"/api/mpesa/callback/{token}/c2b/confirmation", json=payload).json()["ResultCode"] == 0
    items = client.get("/api/mpesa/feed").json()["items"]
    assert [i["trans_id"] for i in items].count("TKA1BC2DE3") == 1
    assert items[0]["phone"].startswith("254700") and "***" in items[0]["phone"]  # masked
    assert anon.post(f"/api/mpesa/callback/{token}/stk", json={"garbage": True}).json()["ResultCode"] == 0
    evil = {**payload, "TransID": "X');alert(1);//"}
    anon.post(f"/api/mpesa/callback/{token}/c2b/confirmation", json=evil)
    assert all("alert" not in (i["trans_id"] or "") for i in client.get("/api/mpesa/feed").json()["items"])


# ---------------------------------------------------------------- shifts
def test_shift_cash_up_detects_shortage(client, owner):
    p = add_product(client, selling_price=100, stock_qty=50)
    assert client.post("/api/shifts/open", json={"opening_float": 1000}).status_code == 200
    assert client.post("/api/shifts/open", json={"opening_float": 1000}).status_code == 400
    client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 5}]})  # 500 cash
    client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 2}], "payment_method": "mpesa", "payment_ref": "SAB1CD2EF3"})
    live = client.get("/api/shifts/current").json()
    assert live["expected_cash"] == 1500 and live["mpesa"] == 200 and live["sales_count"] == 2
    closed = client.post("/api/shifts/close", json={"counted_cash": 1300, "notes": "busy day"}).json()
    assert closed["variance"] == -200 and closed["status"] == "closed"
    alerts = client.get("/api/notifications?kind=cash_variance").json()
    assert alerts and "SHORT" in alerts[0]["body"]
    lw = client.get("/api/loss-watch").json()
    me = next(s for s in lw["staff"] if s["role"] == "owner")
    assert me["short_shifts"] == 1 and me["risk"] > 0


# ---------------------------------------------------------------- automation
def test_daily_summary_and_reminders_are_idempotent(client, owner):
    from app import automation
    from app.db import Business, Customer, Sale, SessionLocal, utcnow
    p = add_product(client, selling_price=100, stock_qty=50)
    c = client.post("/api/customers", json={"name": "Mzee Kamau", "phone": "0700987654"}).json()
    s = client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 3}], "customer_id": c["id"], "payment_method": "credit"}).json()
    client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 2}]})
    r = client.put("/api/automation/settings", json={"owner_phone": "0711000111", "daily_summary": True, "summary_hour": 0,
                                                    "debt_reminders": True, "reminder_after_days": 7})
    assert r.status_code == 200
    with SessionLocal() as db:
        db.get(Sale, s["id"]).created_at = utcnow() - timedelta(days=10)
        db.commit()
    from zoneinfo import ZoneInfo
    from datetime import datetime
    noon_utc = datetime.now(ZoneInfo("Africa/Nairobi")).replace(hour=12, minute=0).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    for _ in range(2):
        with SessionLocal() as db:
            b = db.get(Business, owner["business"]["id"])
            automation.run_business_jobs(db, b, noon_utc)
            db.commit()
            automation.dispatch(db)
    out = client.get("/api/notifications?outbox=true").json()
    kinds = [n["kind"] for n in out]
    assert kinds.count("daily_summary") == 1 and kinds.count("debt_reminder") == 1
    summary = next(n for n in out if n["kind"] == "daily_summary")
    assert "Mauzo" in summary["body"] and summary["status"] == "simulated"  # no provider configured in tests
    reminder = next(n for n in out if n["kind"] == "debt_reminder")
    assert "Mzee" in reminder["body"] and "300" in reminder["body"]
    with SessionLocal() as db:
        assert db.get(Customer, c["id"]).reminders_sent == 1


def test_reminders_only_in_polite_hours(client, owner):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app import automation
    from app.db import Business, Sale, SessionLocal, utcnow
    p = add_product(client, selling_price=100, stock_qty=50)
    c = client.post("/api/customers", json={"name": "Night Owl", "phone": "0700111222"}).json()
    s = client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 1}], "customer_id": c["id"], "payment_method": "credit"}).json()
    client.put("/api/automation/settings", json={"debt_reminders": True, "daily_summary": False})
    with SessionLocal() as db:
        db.get(Sale, s["id"]).created_at = utcnow() - timedelta(days=30)
        db.commit()
    night = datetime.now(ZoneInfo("Africa/Nairobi")).replace(hour=22).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    with SessionLocal() as db:
        assert automation.run_business_jobs(db, db.get(Business, owner["business"]["id"]), night)["reminders"] == 0


def test_price_coach_on_cost_increase(client, owner):
    p = add_product(client, cost_price=150, selling_price=180, stock_qty=10)
    r = client.post(f"/api/products/{p['id']}/stock", json={"quantity": 10, "unit_cost": 170}).json()
    sug = r["price_suggestion"]
    assert sug and sug["suggested_price"] > 180 and sug["new_margin_pct"] < sug["old_margin_pct"]
    assert client.get("/api/notifications?kind=price_alert").json()


# ---------------------------------------------------------------- intelligence
def test_duka_score_and_lender_link(client, owner):
    p = add_product(client, selling_price=100, cost_price=70, stock_qty=100)
    for _ in range(3):
        client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 2}]})
    s = client.get("/api/score").json()
    assert 0 <= s["score"] <= 100 and s["grade"] in "ABCD" and set(s["components"]) >= {"consistency", "profitability", "collections"}
    assert s["ready"] is False  # needs 30 days
    link = client.post("/api/score/share", json={"days": 7}).json()
    path = link["url"].split("://", 1)[1].split("/", 1)[1]
    anon = new_client()
    page = anon.get("/" + path)
    assert page.status_code == 200 and "Verified by Duka" in page.text and link["verification_code"] in page.text
    client.post("/api/score/revoke")
    assert anon.get("/" + path).status_code == 404
    assert anon.get("/verify/forged.token").status_code == 404


def test_benchmarks_need_three_other_shops(client):
    shops = []
    for price in (180, 185, 190, 200):
        c = new_client()
        register(c, f"Shop {price}")
        c.put("/api/automation/settings", json={"benchmark_opt_in": True, "county": "Nairobi", "daily_summary": False})
        add_product(c, name="Jogoo 2kg", barcode="6161100009999", selling_price=price, cost_price=150)
        shops.append(c)
    me = shops[-1]
    items = me.get("/api/benchmarks").json()["items"]
    assert items and items[0]["median"] == 185 and items[0]["shops"] == 3 and items[0]["position"] == "above"
    # a shop that hasn't opted in sees nothing
    outsider = new_client()
    register(outsider)
    add_product(outsider, name="Jogoo 2kg", barcode="6161100009999", selling_price=170, cost_price=150)
    assert outsider.get("/api/benchmarks").json()["items"] == []


def test_quick_entry_sale_expense_receive_payment(client, owner):
    unga = add_product(client, name="Jogoo Maize Flour 2kg", category="Flour", selling_price=185, cost_price=155, stock_qty=20)
    sugar = add_product(client, name="Mumias Sugar 1kg", category="Sugar", selling_price=190, cost_price=160, stock_qty=20)
    milk = add_product(client, name="Brookside Milk 500ml", category="Dairy", selling_price=65, cost_price=55, stock_qty=20)
    p = client.post("/api/quick-entry/parse", json={"text": "sold 2 unga 1 sukari mpesa"}).json()
    assert p["intent"] == "sale" and p["payment_method"] == "mpesa"
    assert {(l["product_id"], l["quantity"]) for l in p["lines"]} == {(unga["id"], 2), (sugar["id"], 1)}
    p2 = client.post("/api/quick-entry/parse", json={"text": "nimeuza maziwa 3, unga 1"}).json()
    assert {(l["product_id"], l["quantity"]) for l in p2["lines"]} == {(milk["id"], 3), (unga["id"], 1)}
    done = client.post("/api/quick-entry/execute", json={"intent": "sale", "lines": [{"product_id": l["product_id"], "quantity": l["quantity"]} for l in p2["lines"]],
                                                         "payment_method": "cash", "client_ref": "qe-1"}).json()
    assert done["sale"]["total"] == 3 * 65 + 185

    e = client.post("/api/quick-entry/parse", json={"text": "nimelipa stima 1500"}).json()
    assert e["intent"] == "expense" and e["expense"]["category"] == "Electricity" and e["expense"]["amount"] == 1500
    assert client.post("/api/quick-entry/execute", json={"intent": "expense", "expense": e["expense"]}).status_code == 200

    rcv = client.post("/api/quick-entry/parse", json={"text": "received 24 milk"}).json()
    assert rcv["intent"] == "receive" and rcv["lines"][0]["product_id"] == milk["id"] and rcv["lines"][0]["quantity"] == 24

    c = client.post("/api/customers", json={"name": "Kamau Njoroge", "phone": "0722000000"}).json()
    client.post("/api/sales", json={"items": [{"product_id": sugar["id"], "quantity": 3}], "customer_id": c["id"], "payment_method": "credit"})
    pay = client.post("/api/quick-entry/parse", json={"text": "Kamau paid 400 mpesa"}).json()
    assert pay["intent"] == "payment" and pay["payment"]["amount"] == 400 and pay["payment"]["method"] == "mpesa"
    client.post("/api/quick-entry/execute", json={"intent": "payment", "payment": pay["payment"]})
    assert client.get(f"/api/customers/{c['id']}/statement").json()["customer"]["balance"] == 170

    credit = client.post("/api/quick-entry/parse", json={"text": "sold 1 sugar to kamau on credit"}).json()
    assert credit["payment_method"] == "credit" and credit["customer_id"] == c["id"]
    nonsense = client.post("/api/quick-entry/parse", json={"text": "blah blah"}).json()
    assert nonsense["intent"] == "error"


def test_cashier_limits_on_smart_features(client, owner):
    email = f"cash-{uuid.uuid4().hex[:6]}@example.com"
    client.post("/api/staff", json={"name": "Till Two", "email": email, "password": PASSWORD, "role": "cashier"})
    cashier = new_client()
    cashier.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    for path in ("/api/score", "/api/loss-watch", "/api/automation/settings", "/api/mpesa/settings", "/api/notifications"):
        assert cashier.get(path).status_code == 403, path
    assert cashier.post("/api/quick-entry/parse", json={"text": "expense 500 transport"}).json()["intent"] == "error"
    assert cashier.post("/api/shifts/open", json={"opening_float": 500}).status_code == 200
