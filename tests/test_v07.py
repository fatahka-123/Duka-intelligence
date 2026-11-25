"""v0.7 messaging: per-shop & shared keys, fallback, delivery reports, broadcasts, receipts, retries."""
import hashlib
import hmac
import json
import uuid
from unittest import mock

import httpx
import pytest

from conftest import PASSWORD, add_product, new_client, register

AT_OK = {"SMSMessageData": {"Message": "Sent to 1/1", "Recipients": [{"statusCode": 101, "number": "+254712345678", "status": "Success",
                                                                     "cost": "KES 0.8000", "messageId": "ATXid_abc"}]}}


class FakeHttp:
    """Pretends to be Meta and Africa's Talking; records what was sent."""
    def __init__(self, wa_fail=False):
        self.sent, self.wa_fail = [], wa_fail

    def post(self, url, **kw):
        if "graph.facebook.com" in url:
            self.sent.append(("whatsapp", kw["json"]["to"], kw["json"]))
            if self.wa_fail:
                return httpx.Response(400, json={"error": {"message": "Template required (outside 24h window)"}})
            return httpx.Response(200, json={"messages": [{"id": f"wamid.{len(self.sent)}"}]})
        self.sent.append(("sms", kw["data"]["to"], kw["data"]))
        return httpx.Response(201, json=AT_OK)

    def get(self, url, **kw):
        if "graph.facebook.com" in url:
            return httpx.Response(200, json={"display_phone_number": "+254 700 000000", "verified_name": "Umoja", "quality_rating": "GREEN"})
        return httpx.Response(200, json={"UserData": {"balance": "KES 512.40"}})


@pytest.fixture
def fake():
    f = FakeHttp()
    with mock.patch("app.messaging.httpx.post", side_effect=f.post), mock.patch("app.messaging.httpx.get", side_effect=f.get):
        yield f


def own_keys(client, **kw):
    body = {"source": "own", "notify_channel": "whatsapp", "sms_fallback": True, "wa_token": "EAAG-token", "wa_phone_id": "1234567890",
            "at_username": "umoja", "at_api_key": "atsk_secret", "at_sender_id": "UMOJA", **kw}
    r = client.put("/api/messaging/settings", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_own_keys_are_encrypted_masked_and_tested(client, owner, fake):
    s = own_keys(client)
    assert s["effective"] == {"source": "own", "whatsapp": True, "sms": True}
    assert "atsk_secret" not in json.dumps(s) and "EAAG-token" not in json.dumps(s) and s["own"]["at_api_key"].startswith("••")
    from app.db import Business, SessionLocal
    with SessionLocal() as db:
        assert "atsk_secret" not in db.get(Business, owner["business"]["id"]).at_api_key
    t = client.post("/api/messaging/test").json()
    assert t["sms"]["ok"] and "512.40" in t["sms"]["detail"] and t["whatsapp"]["ok"] and "Umoja" in t["whatsapp"]["detail"]
    # saving again with the masked value keeps the key
    own_keys(client, at_api_key=s["own"]["at_api_key"], wa_token=None)
    assert client.post("/api/messaging/test").json()["sms"]["ok"]


def test_test_send_and_whatsapp_falls_back_to_sms(client, owner):
    own_keys(client)
    f = FakeHttp(wa_fail=True)
    with mock.patch("app.messaging.httpx.post", side_effect=f.post):
        r = client.post("/api/messaging/test-send", json={"phone": "0712345678", "channel": "whatsapp"}).json()
    assert r["status"] == "sent" and r["channel"] == "sms" and "instead" in r["note"]
    assert [x[0] for x in f.sent] == ["whatsapp", "sms"] and f.sent[1][1] == "+254712345678"
    out = client.get("/api/notifications?outbox=true").json()
    assert out[0]["kind"] == "test" and out[0]["channel"] == "sms"


def test_no_fallback_when_switched_off(client, owner):
    own_keys(client, sms_fallback=False)
    f = FakeHttp(wa_fail=True)
    with mock.patch("app.messaging.httpx.post", side_effect=f.post):
        r = client.post("/api/messaging/test-send", json={"phone": "0712345678", "channel": "whatsapp"})
    assert r.status_code == 400 and "Template required" in r.json()["detail"] and len(f.sent) == 1


def test_without_keys_messages_are_simulated(client, owner):
    client.put("/api/messaging/settings", json={"source": "own"})
    r = client.post("/api/messaging/test-send", json={"phone": "0712345678"}).json()
    assert r["status"] == "simulated"


def test_shared_platform_account_and_admin_switch(fake, monkeypatch):
    admin = new_client(); info = register(admin, "Provider HQ")
    import app.security as sec
    monkeypatch.setattr(sec, "PLATFORM_ADMINS", {info["email"]})
    assert admin.put("/api/admin/messaging", json={"at_username": "provider", "at_api_key": "plat_key", "at_sender_id": "GROVIA"}).json()["sms"]
    assert admin.post("/api/admin/messaging/test").json()["sms"]["ok"]
    shop = new_client(); s = register(shop, "Client Shop")
    st = shop.get("/api/messaging/settings").json()
    assert st["source"] == "platform" and st["effective"]["source"] == "platform" and st["effective"]["sms"]
    r = shop.post("/api/messaging/test-send", json={"phone": "0712345678", "channel": "sms"}).json()
    assert r["status"] == "sent" and fake.sent[-1][2]["from"] == "GROVIA"
    # admin switches shared messaging off for this shop → nothing leaves
    admin.post(f"/api/admin/businesses/{s['business']['id']}/messaging", json={"allowed": False})
    assert shop.post("/api/messaging/test-send", json={"phone": "0712345678", "channel": "sms"}).json()["status"] == "simulated"
    usage = admin.get("/api/admin/messaging").json()["shops"]
    assert any(x["name"] == "Client Shop" and x["sms"] == 1 for x in usage)
    assert shop.get("/api/admin/messaging").status_code == 404


def test_delivery_reports_update_status(client, owner, fake):
    s = own_keys(client, wa_app_secret="shh")
    client.post("/api/messaging/test-send", json={"phone": "0712345678", "channel": "whatsapp"})
    hooks = client.get("/api/messaging/settings").json()["hooks"]
    url = hooks["whatsapp_webhook"].split("://", 1)[1].split("/", 1)[1]
    anon = new_client()
    v = anon.get(f"/{url}", params={"hub.mode": "subscribe", "hub.verify_token": hooks["whatsapp_verify_token"], "hub.challenge": "42"})
    assert v.status_code == 200 and v.text == "42"
    assert anon.get(f"/{url}", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "42"}).status_code == 403
    body = json.dumps({"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.1", "status": "read"}]}}]}]}).encode()
    assert anon.post(f"/{url}", content=body, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 403
    sig = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    assert anon.post(f"/{url}", content=body, headers={"x-hub-signature-256": sig, "content-type": "application/json"}).status_code == 200
    assert client.get("/api/notifications?outbox=true").json()[0]["status"] == "read"
    # Africa's Talking delivery report for an SMS
    client.post("/api/messaging/test-send", json={"phone": "0712345678", "channel": "sms"})
    dlr = hooks["sms_delivery_reports"].split("://", 1)[1].split("/", 1)[1]
    rr = anon.post(f"/{dlr}", data={"id": "ATXid_abc", "status": "Failed", "failureReason": "UserInBlacklist"})
    assert rr.status_code == 200
    top = [n for n in client.get("/api/notifications?outbox=true").json() if n["channel"] == "sms"][0]
    assert top["status"] == "failed" and "Blacklist" in top["error"]
    assert anon.post("/api/messaging/hooks/" + "x" * 30 + "/sms-delivery", data={"id": "1"}).status_code == 404


def test_broadcast_respects_audience_and_opt_out(client, owner, fake):
    own_keys(client)
    p = add_product(client, selling_price=100, stock_qty=50)
    a = client.post("/api/customers", json={"name": "Amina Otieno", "phone": "0711111111"}).json()
    b = client.post("/api/customers", json={"name": "Brian K", "phone": "0722222222"}).json()
    client.post("/api/customers", json={"name": "No Phone"})
    c = client.post("/api/customers", json={"name": "Opted Out", "phone": "0733333333", "marketing_opt_out": True}).json()
    client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 1}], "payment_method": "credit", "customer_id": a["id"]})
    assert client.get("/api/messaging/audience?audience=owing").json()["count"] == 1
    aud = client.get("/api/messaging/audience?audience=all").json()
    assert aud["count"] == 2 and aud["opted_out"] == 1
    r = client.post("/api/messaging/broadcast", json={"audience": "all", "message": "Habari {name}! Unga offer this week.", "channel": "sms"}).json()
    assert r["queued"] == 2 and r["sent_now"] == 2 and r["opted_out"] == 1
    bodies = [x[2]["message"] for x in fake.sent]
    assert any(m.startswith("Habari Amina!") for m in bodies) and all("0733333333" not in x[1] for x in fake.sent)
    assert c["marketing_opt_out"] is True


def test_send_receipt_and_retry_failed(client, owner):
    own_keys(client)
    p = add_product(client, selling_price=150, stock_qty=5)
    sale = client.post("/api/sales", json={"items": [{"product_id": p["id"], "quantity": 2}]}).json()
    f = FakeHttp()
    with mock.patch("app.messaging.httpx.post", side_effect=f.post):
        r = client.post(f"/api/sales/{sale['id']}/send-receipt", json={"phone": "0712 345 678", "channel": "whatsapp"}).json()
    assert r["status"] == "sent" and "TOTAL KSh 300" in f.sent[0][2]["text"]["body"]
    # a failing SMS can be retried once the provider recovers
    with mock.patch("app.messaging.httpx.post", return_value=httpx.Response(500, text="down")):
        bad = client.post(f"/api/sales/{sale['id']}/send-receipt", json={"phone": "0712345678", "channel": "sms"})
    assert bad.status_code == 400
    failed = [n for n in client.get("/api/notifications?outbox=true").json() if n["status"] == "failed"][0]
    with mock.patch("app.messaging.httpx.post", side_effect=FakeHttp().post):
        assert client.post(f"/api/notifications/{failed['id']}/retry").json()["status"] == "sent"


def test_cashier_cannot_change_messaging_or_broadcast(client, owner):
    client.post("/api/staff", json={"name": "Cash Ier", "email": f"c-{uuid.uuid4().hex[:6]}@example.com", "password": PASSWORD, "role": "cashier"})
    email = [u for u in client.get("/api/staff").json() if u["role"] == "cashier"][0]["email"]
    c2 = new_client(); c2.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert c2.put("/api/messaging/settings", json={"source": "off"}).status_code == 403
    assert c2.post("/api/messaging/broadcast", json={"audience": "all", "message": "hello all"}).status_code == 403


def test_sms_parts():
    from app.messaging import sms_parts
    assert sms_parts("a" * 160) == 1 and sms_parts("a" * 161) == 2 and sms_parts("🙂" * 70) == 1 and sms_parts("🙂" * 71) == 2


def test_a_shops_webhook_cannot_touch_another_shops_messages(fake):
    a = new_client(); register(a, "Shop A"); own_keys(a)
    b = new_client(); register(b, "Shop B"); own_keys(b)
    b.post("/api/messaging/test-send", json={"phone": "0712345678", "channel": "sms"})  # ref ATXid_abc in shop B
    hook_a = a.get("/api/messaging/settings").json()["hooks"]["sms_delivery_reports"].split("://", 1)[1].split("/", 1)[1]
    new_client().post(f"/{hook_a}", data={"id": "ATXid_abc", "status": "Failed"})
    assert b.get("/api/notifications?outbox=true").json()[0]["status"] == "sent"
