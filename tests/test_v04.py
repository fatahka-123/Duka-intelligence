"""Tests for v0.4: returns & refunds, purchase orders, password reset, M-Pesa connection test, backups."""
from unittest import mock

import httpx

from conftest import PASSWORD, add_product, new_client, register


def sale(client, pid, qty, **kw):
    r = client.post("/api/sales", json={"items": [{"product_id": pid, "quantity": qty}], **kw})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- returns
def test_partial_return_refunds_restocks_and_fixes_reports(client, owner):
    p = add_product(client, cost_price=100, selling_price=150, stock_qty=10)
    s = sale(client, p["id"], 4)
    r = client.post(f"/api/sales/{s['id']}/return", json={"items": [{"product_id": p["id"], "quantity": 1}], "reason": "Changed mind"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["refund"] == 150 and out["total"] == 450 and out["paid"] == 450
    assert out["items"][0]["quantity"] == 3 and out["returns"]
    assert any(pmt["amount"] == -150 for pmt in out["payments"])
    stock = [x for x in client.get("/api/products").json() if x["id"] == p["id"]][0]["stock_qty"]
    assert stock == 7
    rep = client.get("/api/reports/summary?period=today").json()
    assert rep["revenue"] == 450 and rep["cost_of_goods_sold"] == 300
    # can't return more than is left on the sale
    too_many = client.post(f"/api/sales/{s['id']}/return", json={"items": [{"product_id": p["id"], "quantity": 4}], "reason": "x again"})
    assert too_many.status_code == 400


def test_return_on_credit_sale_reduces_debt_without_refund(client, owner):
    p = add_product(client, selling_price=100, stock_qty=10)
    c = client.post("/api/customers", json={"name": "Mama Mboga", "phone": "0711000000"}).json()
    s = sale(client, p["id"], 3, payment_method="credit", customer_id=c["id"])
    out = client.post(f"/api/sales/{s['id']}/return", json={"items": [{"product_id": p["id"], "quantity": 2}], "reason": "Expired",
                                                            "restock": False}).json()
    assert out["refund"] == 0 and out["balance"] == 100
    stock = [x for x in client.get("/api/products").json() if x["id"] == p["id"]][0]["stock_qty"]
    assert stock == 7  # damaged goods not put back


def test_cash_refund_lowers_expected_cash_at_cashup(client, owner):
    client.post("/api/shifts/open", json={"opening_float": 1000})
    p = add_product(client, selling_price=200, stock_qty=10)
    s = sale(client, p["id"], 2)
    client.post(f"/api/sales/{s['id']}/return", json={"items": [{"product_id": p["id"], "quantity": 1}], "reason": "Wrong size"})
    assert client.get("/api/shifts/current").json()["expected_cash"] == 1200


def test_cashier_cannot_record_returns(client, owner):
    p = add_product(client, stock_qty=5)
    s = sale(client, p["id"], 1)
    client.post("/api/staff", json={"name": "Cash Ier", "email": "cashier-ret@example.com", "password": PASSWORD, "role": "cashier"})
    c2 = new_client()
    c2.post("/api/auth/login", json={"email": "cashier-ret@example.com", "password": PASSWORD})
    assert c2.post(f"/api/sales/{s['id']}/return", json={"items": [{"product_id": p["id"], "quantity": 1}], "reason": "nope"}).status_code == 403


# ---------------------------------------------------------------- purchase orders
def test_purchase_order_lifecycle(client, owner):
    sup = client.post("/api/suppliers", json={"name": "Bidco", "phone": "0720555000"}).json()
    p = add_product(client, cost_price=100, selling_price=150, stock_qty=1, reorder_level=5)
    assert any(x["product_id"] == p["id"] for x in client.get("/api/purchase-orders/suggest").json())
    po = client.post("/api/purchase-orders", json={"supplier_id": sup["id"], "items": [{"product_id": p["id"], "quantity": 20, "unit_cost": 110}],
                                                   "notes": "Deliver Friday"}).json()
    assert po["status"] == "draft" and po["total"] == 2200 and po["reference"].startswith("PO-")
    msg = client.get(f"/api/purchase-orders/{po['id']}/message").json()
    assert "20" in msg["text"] and msg["phone"] == "0720555000"
    assert client.post(f"/api/purchase-orders/{po['id']}/sent").json()["status"] == "sent"
    item = po["items"][0]
    rec = client.post(f"/api/purchase-orders/{po['id']}/receive", json={"items": [{"item_id": item["id"], "quantity": 18, "unit_cost": 110}]}).json()
    assert rec["status"] == "received" and rec["items"][0]["received_qty"] == 18 and rec["total"] == 1980
    prod = [x for x in client.get("/api/products").json() if x["id"] == p["id"]][0]
    assert prod["stock_qty"] == 19 and 100 < prod["cost_price"] <= 110
    assert any(x["quantity"] == 18 for x in client.get(f"/api/suppliers/{sup['id']}/purchases").json())
    assert client.post(f"/api/purchase-orders/{po['id']}/receive", json={}).status_code == 400  # not twice
    assert client.post(f"/api/purchase-orders/{po['id']}/cancel").status_code == 400


def test_purchase_orders_are_private_to_each_business(client, owner):
    p = add_product(client)
    po = client.post("/api/purchase-orders", json={"items": [{"product_id": p["id"], "quantity": 5}]}).json()
    other = new_client(); register(other, "Other Shop")
    assert other.get(f"/api/purchase-orders/{po['id']}/message").status_code == 404
    assert other.post(f"/api/purchase-orders/{po['id']}/receive", json={}).status_code == 404
    assert other.post("/api/purchase-orders", json={"items": [{"product_id": p["id"], "quantity": 5}]}).status_code == 404


# ---------------------------------------------------------------- password reset
def test_forgot_password_without_sms_provider_explains_options(client, owner):
    r = client.post("/api/auth/forgot", json={"email": owner["email"]})
    assert r.status_code == 200 and "reset_password" in r.json()["message"]


def test_password_reset_by_sms_code(client, owner, monkeypatch):
    client.put("/api/automation/settings", json={"owner_phone": "0712000111"})
    monkeypatch.setenv("AT_USERNAME", "sandbox"); monkeypatch.setenv("AT_API_KEY", "k")
    sent = {}
    with mock.patch("app.automation.send_sms", side_effect=lambda to, body: sent.update(to=to, body=body)):
        anon = new_client()
        assert anon.post("/api/auth/forgot", json={"email": owner["email"]}).status_code == 200
        assert anon.post("/api/auth/forgot", json={"email": "nobody@example.com"}).status_code == 200  # no account leak
    assert sent["to"] == "254712000111"
    code = sent["body"].split("code is ")[1][:6]
    wrong = "000000" if code != "000000" else "111111"
    assert anon.post("/api/auth/reset", json={"email": owner["email"], "code": wrong, "new_password": "brand-new-password-1"}).status_code == 400
    ok = anon.post("/api/auth/reset", json={"email": owner["email"], "code": code, "new_password": "brand-new-password-1"})
    assert ok.status_code == 200, ok.text
    assert anon.post("/api/auth/reset", json={"email": owner["email"], "code": code, "new_password": "another-password-22"}).status_code == 400
    assert anon.post("/api/auth/login", json={"email": owner["email"], "password": "brand-new-password-1"}).status_code == 200
    assert client.get("/api/auth/me").status_code == 401  # old sessions signed out


def test_reset_code_locks_after_five_wrong_tries(client, owner, monkeypatch):
    client.put("/api/automation/settings", json={"owner_phone": "0712000222"})
    monkeypatch.setenv("AT_USERNAME", "sandbox"); monkeypatch.setenv("AT_API_KEY", "k")
    sent = {}
    with mock.patch("app.automation.send_sms", side_effect=lambda to, body: sent.update(body=body)):
        client.post("/api/auth/forgot", json={"email": owner["email"]})
    code = sent["body"].split("code is ")[1][:6]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        client.post("/api/auth/reset", json={"email": owner["email"], "code": wrong, "new_password": "brand-new-password-1"})
    assert client.post("/api/auth/reset", json={"email": owner["email"], "code": code, "new_password": "brand-new-password-1"}).status_code == 400


# ---------------------------------------------------------------- M-Pesa connection test
def test_mpesa_test_in_simulator_says_no_real_prompts(client, owner):
    client.put("/api/mpesa/settings", json={"mode": "simulator", "shortcode": "123456", "type": "till"})
    r = client.post("/api/mpesa/test").json()
    assert r["ok"] and "real phones" in r["message"]
    s = client.get("/api/mpesa/settings").json()
    assert s["sandbox_defaults"]["shortcode"] == "174379"


def test_mpesa_test_reports_bad_keys_and_good_keys(client, owner):
    client.put("/api/mpesa/settings", json={"mode": "sandbox", "shortcode": "174379", "type": "paybill",
                                            "consumer_key": "k", "consumer_secret": "s", "passkey": "p"})
    with mock.patch("app.mpesa.httpx.get", return_value=httpx.Response(400, json={})):
        bad = client.post("/api/mpesa/test")
    assert bad.status_code == 400 and "rejected" in bad.json()["detail"]
    with mock.patch("app.mpesa.httpx.get", return_value=httpx.Response(200, json={"access_token": "t"})):
        good = client.post("/api/mpesa/test")
    assert good.status_code == 200 and good.json()["ok"]
    with mock.patch("app.mpesa.httpx.get", side_effect=httpx.ConnectError("down")):
        down = client.post("/api/mpesa/test")
    assert down.status_code == 400 and "reach Safaricom" in down.json()["detail"]


def test_stk_push_sends_correct_sandbox_request(client, owner):
    client.put("/api/mpesa/settings", json={"mode": "sandbox", "shortcode": "174379", "type": "paybill",
                                            "consumer_key": "k", "consumer_secret": "s", "passkey": "p"})
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured.update(json)
        return httpx.Response(200, json={"ResponseCode": "0", "CheckoutRequestID": "ws_CO_1"})
    with mock.patch("app.mpesa.httpx.get", return_value=httpx.Response(200, json={"access_token": "t"})), \
         mock.patch("app.mpesa.httpx.post", side_effect=fake_post):
        r = client.post("/api/mpesa/stk", json={"phone": "0712 345 678", "amount": 99.5})
    assert r.status_code == 200, r.text
    assert captured["PhoneNumber"] == "254712345678" and captured["Amount"] == 100
    assert captured["TransactionType"] == "CustomerPayBillOnline" and captured["BusinessShortCode"] == "174379"
    assert captured["CallBackURL"].endswith("/stk") and len(captured["Timestamp"]) == 14


# ---------------------------------------------------------------- backup
def test_backup_has_data_and_no_secrets(client, owner):
    client.put("/api/mpesa/settings", json={"mode": "sandbox", "shortcode": "174379", "type": "paybill",
                                            "consumer_key": "topsecretkey", "consumer_secret": "topsecretsecret", "passkey": "pk"})
    p = add_product(client)
    sale(client, p["id"], 1)
    r = client.get("/api/export/backup.json")
    assert r.status_code == 200
    data = r.json()
    assert data["products"] and data["sales"] and data["sale_items"] and data["payments"]
    assert "topsecret" not in r.text and "password_hash" not in r.text
