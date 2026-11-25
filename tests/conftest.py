import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

# Isolated throwaway database — tests never touch your local duka.db
_tmp = Path(tempfile.mkdtemp()) / "test.db"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ.setdefault("LOGIN_MAX_FAILURES", "5")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

PASSWORD = "a-very-secure-password"


def new_client() -> TestClient:
    c = TestClient(app)
    c.__enter__()  # run lifespan (creates tables)
    return c


def register(client: TestClient, business="Test Retail") -> dict:
    email = f"owner-{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/api/auth/register", json={"business_name": business, "name": "Test Owner", "email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {**r.json(), "email": email}


def add_product(client: TestClient, **kw) -> dict:
    body = {"name": "Maize Flour 2kg", "cost_price": 120, "selling_price": 150, "stock_qty": 10, "reorder_level": 2, **kw}
    r = client.post("/api/products", json=body)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def client():
    return new_client()


@pytest.fixture
def owner(client):
    return register(client)


@pytest.fixture(autouse=True)
def _reset_throttles():
    from app.security import ip_throttle, login_throttle
    for t in (ip_throttle, login_throttle):
        t._hits.clear()
