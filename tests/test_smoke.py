"""Original v0.1 smoke tests (kept to prove API compatibility)."""
from conftest import add_product, register


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["database"] is True


def test_register_product_sale_and_report(client, owner):
    pid = add_product(client, sku="MF2", category="Food")["id"]
    sale = client.post("/api/sales", json={"items": [{"product_id": pid, "quantity": 2}], "payment_method": "cash"})
    assert sale.status_code == 200, sale.text
    assert sale.json()["total"] == 300
    assert client.get("/api/products").json()[0]["stock_qty"] == 8
    report = client.get("/api/reports/profit").json()
    assert report["revenue"] == 300 and report["cost_of_goods_sold"] == 240 and report["gross_profit"] == 60


def test_tenant_isolation_and_partial_payment(client):
    register(client, "Shop One")
    p = add_product(client, name="Rice 1kg", cost_price=100, selling_price=130, stock_qty=5)
    cust = client.post("/api/customers", json={"name": "Mama Njeri", "phone": "0711000000"}).json()
    sale = client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 2}], "amount_paid": 100, "customer_id": cust["id"]}).json()
    payment = client.post(f"/api/sales/{sale['id']}/payment", json={"amount": 160, "payment_method": "mpesa", "payment_ref": "QJK12AB34C"})
    assert payment.status_code == 200, payment.text
    assert payment.json()["balance"] == 0
    assert len(payment.json()["payments"]) == 2
    client.post("/api/auth/logout")
    register(client, "Shop Two")
    assert client.get("/api/products").json() == []
    assert client.post(f"/api/sales/{sale['id']}/payment", json={"amount": 1}).status_code == 404
    assert client.get(f"/api/sales/{sale['id']}").status_code == 404
    assert client.get(f"/api/customers/{cust['id']}/statement").status_code == 404
    assert client.post(f"/api/products/{p['id']}/stock", json={"quantity": 1}).status_code == 404
