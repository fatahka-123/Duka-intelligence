"""Grovia — FastAPI application entry point."""
from __future__ import annotations

import asyncio
import logging
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .config import DEMO_STATE, AUTO_CREATE_TABLES, COOKIE_SECURE, ENABLE_SCHEDULER, SEED_DEMO, SEED_DEMO_DAYS, VERSION, assert_safe_for_production
from .db import Base, engine
from .automation import scheduler_loop
from .routes import admin, auth, business, inventory, messaging, purchasing, sales, smart

BASE = Path(__file__).resolve().parent
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

CSP = "; ".join([
    "default-src 'self'",
    # inline handlers are still used by the UI; remove 'unsafe-inline' once they are migrated to addEventListener
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
])


def seed_demo_if_empty() -> None:
    from sqlalchemy import func, select
    from .db import SessionLocal, User
    from .seed import seed
    try:
        with SessionLocal() as db:
            if db.scalar(select(func.count(User.id)).where(User.email == "demo@duka.app")):
                DEMO_STATE["state"] = "ready"
                return
            if db.scalar(select(func.count(User.id))):
                DEMO_STATE["state"] = "off"  # real accounts exist: never inject demo data into a live install
                return
        DEMO_STATE["state"] = "seeding"
        seed("demo@duka.app", "demo-password-123", SEED_DEMO_DAYS)
        DEMO_STATE["state"] = "ready"
    except Exception:  # noqa: BLE001 — a demo seed must never stop the app from serving
        DEMO_STATE["state"] = "failed"
        logging.getLogger("duka").exception("demo seed failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    assert_safe_for_production()
    if AUTO_CREATE_TABLES:
        Base.metadata.create_all(engine)
    if SEED_DEMO:
        asyncio.create_task(asyncio.to_thread(seed_demo_if_empty))
    task = asyncio.create_task(scheduler_loop()) if ENABLE_SCHEDULER else None
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Grovia", version=VERSION, lifespan=lifespan,
              description="Retail operations and business intelligence for African SMEs")


@app.middleware("http")
async def security(request: Request, call_next):
    # CSRF defence in depth (cookie is SameSite=Lax): reject cross-origin state-changing API calls
    if request.method in UNSAFE_METHODS and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
            if urlparse(origin).netloc != host:
                return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
    response = await call_next(request)
    h = response.headers
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "strict-origin-when-cross-origin"
    h["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    h["Content-Security-Policy"] = CSP
    if COOKIE_SECURE:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    path = request.url.path
    if path.startswith("/api/") or path == "/":
        h["Cache-Control"] = "no-store"
    elif path.startswith("/static/"):
        h.setdefault("Cache-Control", "public, max-age=300")
    return response


@app.exception_handler(RequestValidationError)
async def friendly_validation(_: Request, exc: RequestValidationError):
    """Return one readable sentence instead of a list of objects, so the UI can show it directly."""
    messages = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err.get("loc", []) if p not in ("body", "query", "path"))
        msg = err.get("msg", "is invalid").removeprefix("Value error, ")
        messages.append(f"{field.replace('_', ' ')}: {msg}" if field else msg)
    return JSONResponse({"detail": "; ".join(messages[:3]), "errors": jsonable_encoder(exc.errors(), custom_encoder={Exception: str})}, status_code=422)


for module in (auth, inventory, sales, business, smart, purchasing, admin, messaging):
    app.include_router(module.router)

app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(BASE / "static" / "index.html", media_type="text/html")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    # Served from the root so the worker can control the whole app scope
    return FileResponse(BASE / "static" / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return JSONResponse({"status": "ok" if db_ok else "degraded", "database": db_ok, "service": "duka-intelligence",
                         "version": VERSION, "demo": DEMO_STATE["state"]}, status_code=200 if db_ok else 503)
