"""Runtime configuration, read once from environment variables."""
from __future__ import annotations

import os

VERSION = "0.4.1"

DEV_SECRET_PREFIX = "dev-only-change-this"


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///./duka.db")
    # Railway/Render/Heroku style URLs -> SQLAlchemy psycopg3 driver
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


DATABASE_URL = _database_url()
IS_SQLITE = DATABASE_URL.startswith("sqlite")
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
SECRET_KEY = os.getenv("DUKA_SECRET_KEY", DEV_SECRET_PREFIX + "-" + "x" * 24)
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "7"))
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Africa/Nairobi")
# Create tables automatically (handy for local SQLite). In production run `alembic upgrade head`.
AUTO_CREATE_TABLES = os.getenv("AUTO_CREATE_TABLES", "true" if IS_SQLITE else "false").lower() == "true"
# Login throttling
LOGIN_MAX_FAILURES = int(os.getenv("LOGIN_MAX_FAILURES", "5"))
LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", "900"))

PAYMENT_METHODS = ("cash", "mpesa", "bank", "card", "credit", "other")
ROLES = ("owner", "manager", "cashier")
ADJUSTMENT_REASONS = ("count", "damaged", "expired", "theft", "own_use", "returned_to_supplier", "other")


def assert_safe_for_production() -> None:
    """Refuse to boot with the dev secret anywhere that is not a local SQLite dev box."""
    insecure_secret = SECRET_KEY.startswith(DEV_SECRET_PREFIX) or len(SECRET_KEY) < 32
    if insecure_secret and (ENVIRONMENT == "production" or not IS_SQLITE):
        raise RuntimeError(
            "DUKA_SECRET_KEY must be set to a unique random value of at least 32 characters "
            "before running against PostgreSQL or with ENVIRONMENT=production."
        )

# --- v0.3
# Public HTTPS URL of this deployment (used to build M-Pesa callback URLs and lender links)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL", "")  # Render sets RENDER_EXTERNAL_URL
# In-process automation scheduler (daily summaries, reminders, alerts). Disable if you use /internal/cron instead.
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"
CRON_SECRET = os.getenv("CRON_SECRET", "")
MPESA_ENFORCE_IPS = os.getenv("MPESA_ENFORCE_IPS", "false").lower() == "true"
# Create the demo shop (demo@duka.app) on first start when the database has no users. For demos only.
SEED_DEMO = os.getenv("SEED_DEMO", "false").lower() == "true"
SEED_DEMO_DAYS = int(os.getenv("SEED_DEMO_DAYS", "90"))
DEMO_STATE = {"state": "off"}  # off | seeding | ready | failed — status of the optional demo seed
