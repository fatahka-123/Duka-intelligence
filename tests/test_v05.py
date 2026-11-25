"""v0.5: platform admin — create client shops, pause/resume, reset logins, forced password change."""
import uuid

import pytest

from conftest import PASSWORD, add_product, new_client, register


@pytest.fixture
def admin(monkeypatch):
    c = new_client()
    info = register(c, "Provider HQ")
    import app.security as sec
    monkeypatch.setattr(sec, "PLATFORM_ADMINS", {info["email"]})
    return c, info


def make_client(c, **kw):
    body = {"business_name": "Baraka Mini Mart", "owner_name": "Baraka Otieno", "email": f"baraka-{uuid.uuid4().hex[:8]}@example.com",
            "phone": "0712345678", **kw}
    r = c.post("/api/admin/businesses", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_admin_area_is_invisible_to_normal_owners(client, owner, admin):
    for path in ("/api/admin/me", "/api/admin/businesses"):
        assert client.get(path).status_code == 404
    assert client.post("/api/admin/businesses", json={"business_name": "X shop", "owner_name": "Xx", "email": "x@example.com"}).status_code == 404
    assert client.get("/api/auth/me").json()["user"]["platform_admin"] is False


def test_admin_creates_client_who_must_set_own_password(admin):
    c, info = admin
    assert c.get("/api/auth/me").json()["user"]["platform_admin"] is True
    r = make_client(c)
    login = r["login"]
    assert len(login["temporary_password"]) >= 10 and r["business"]["owner"]["must_change_password"]
    shop = new_client()
    me = shop.post("/api/auth/login", json={"email": login["email"], "password": login["temporary_password"]}).json()
    assert me["user"]["must_change_password"] and me["user"]["role"] == "owner" and me["business"]["name"] == "Baraka Mini Mart"
    assert shop.post("/api/auth/password", json={"current_password": login["temporary_password"],
                                                  "new_password": login["temporary_password"]}).status_code == 400  # must differ
    assert shop.post("/api/auth/password", json={"current_password": login["temporary_password"], "new_password": PASSWORD}).status_code == 200
    assert shop.get("/api/auth/me").json()["user"]["must_change_password"] is False
    # the new shop is empty and separate from the provider's shop
    assert shop.get("/api/products").json() == []
    add_product(c)
    assert shop.get("/api/products").json() == []
    rows = c.get("/api/admin/businesses").json()
    assert rows["summary"]["shops"] >= 2 and any(b["name"] == "Baraka Mini Mart" for b in rows["businesses"])


def test_duplicate_client_email_rejected(admin):
    c, info = admin
    r = c.post("/api/admin/businesses", json={"business_name": "Dup", "owner_name": "Dup", "email": info["email"]})
    assert r.status_code == 409


def test_pause_blocks_sign_in_and_live_sessions_then_resume(admin):
    c, _ = admin
    r = make_client(c)
    shop = new_client()
    shop.post("/api/auth/login", json={"email": r["login"]["email"], "password": r["login"]["temporary_password"]})
    assert shop.get("/api/products").status_code == 200
    bid = r["business"]["id"]
    assert c.post(f"/api/admin/businesses/{bid}/status", json={"active": False, "note": "Unpaid"}).json()["active"] is False
    blocked = shop.get("/api/products")
    assert blocked.status_code == 403 and "paused" in blocked.json()["detail"]
    again = new_client().post("/api/auth/login", json={"email": r["login"]["email"], "password": r["login"]["temporary_password"]})
    assert again.status_code == 403
    c.post(f"/api/admin/businesses/{bid}/status", json={"active": True})
    assert shop.get("/api/products").status_code == 200


def test_admin_cannot_pause_own_shop(admin):
    c, info = admin
    own = info["business"]["id"]
    assert c.post(f"/api/admin/businesses/{own}/status", json={"active": False}).status_code == 400


def test_admin_password_reset_logs_user_out_and_is_audited(admin):
    c, _ = admin
    r = make_client(c)
    shop = new_client()
    shop.post("/api/auth/login", json={"email": r["login"]["email"], "password": r["login"]["temporary_password"]})
    users = c.get(f"/api/admin/businesses/{r['business']['id']}/users").json()
    new = c.post(f"/api/admin/users/{users[0]['id']}/reset-password").json()
    assert new["temporary_password"] != r["login"]["temporary_password"]
    assert shop.get("/api/products").status_code == 401  # old session revoked
    shop2 = new_client()
    assert shop2.post("/api/auth/login", json={"email": new["email"], "password": new["temporary_password"]}).json()["user"]["must_change_password"]
    log = shop2.get("/api/audit").json()
    assert any(a["action"] == "platform.password_reset" and "by provider" in a["detail"] for a in log)


def test_staff_added_by_owner_must_change_password(client, owner):
    client.post("/api/staff", json={"name": "New Cashier", "email": "nc-v05@example.com", "password": PASSWORD, "role": "cashier"})
    c2 = new_client()
    assert c2.post("/api/auth/login", json={"email": "nc-v05@example.com", "password": PASSWORD}).json()["user"]["must_change_password"]
