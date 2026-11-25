"""Database engine, session and ORM models.

Every business-owned table carries ``business_id``; every query in the API layer
must filter on it. Money is NUMERIC(14,2) and handled with ``Decimal``.
Timestamps are stored as naive UTC.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
    UniqueConstraint, create_engine, false, true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import DATABASE_URL, IS_SQLITE

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if IS_SQLITE else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def utcnow() -> datetime:
    """Naive UTC 'now' (columns are timezone-naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class Base(DeclarativeBase):
    pass


Money = Numeric(14, 2)


class Business(Base):
    __tablename__ = "businesses"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(140))
    phone: Mapped[str] = mapped_column(String(40), default="")
    currency: Mapped[str] = mapped_column(String(3), default="KES")
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Nairobi", server_default="Africa/Nairobi")
    address: Mapped[str] = mapped_column(String(240), default="", server_default="")
    kra_pin: Mapped[str] = mapped_column(String(20), default="", server_default="")
    receipt_footer: Mapped[str] = mapped_column(String(240), default="Asante! Karibu tena.", server_default="Asante! Karibu tena.")
    county: Mapped[str] = mapped_column(String(60), default="", server_default="")
    # --- automation
    owner_phone: Mapped[str] = mapped_column(String(40), default="", server_default="")
    notify_channel: Mapped[str] = mapped_column(String(20), default="whatsapp", server_default="whatsapp")  # whatsapp | sms
    daily_summary: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    summary_hour: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    debt_reminders: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    reminder_after_days: Mapped[int] = mapped_column(Integer, default=7, server_default="7")
    price_alerts: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # --- network & sharing
    benchmark_opt_in: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    share_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # bump to revoke lender links
    # --- M-Pesa (Daraja). Secrets are encrypted at rest (see crypto.py)
    mpesa_mode: Mapped[str] = mapped_column(String(12), default="off", server_default="off")  # off | simulator | sandbox | production
    mpesa_shortcode: Mapped[str] = mapped_column(String(20), default="", server_default="")
    mpesa_type: Mapped[str] = mapped_column(String(10), default="till", server_default="till")  # till | paybill
    mpesa_store_number: Mapped[str] = mapped_column(String(20), default="", server_default="")  # head office number for tills
    mpesa_consumer_key: Mapped[str] = mapped_column(Text, default="", server_default="")
    mpesa_consumer_secret: Mapped[str] = mapped_column(Text, default="", server_default="")
    mpesa_passkey: Mapped[str] = mapped_column(Text, default="", server_default="")
    mpesa_callback_token: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # --- messaging (v0.7). msg_source: platform = use the provider's shared account | own = this shop's keys | off
    msg_source: Mapped[str] = mapped_column(String(10), default="platform", server_default="platform")
    platform_messaging: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())  # admin may revoke
    sms_fallback: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    wa_token: Mapped[str] = mapped_column(Text, default="", server_default="")  # encrypted
    wa_phone_id: Mapped[str] = mapped_column(String(40), default="", server_default="")
    wa_template: Mapped[str] = mapped_column(String(80), default="", server_default="")
    wa_template_lang: Mapped[str] = mapped_column(String(10), default="en", server_default="en")
    wa_app_secret: Mapped[str] = mapped_column(Text, default="", server_default="")  # encrypted; verifies webhook signatures
    at_username: Mapped[str] = mapped_column(String(80), default="", server_default="")
    at_api_key: Mapped[str] = mapped_column(Text, default="", server_default="")  # encrypted
    at_sender_id: Mapped[str] = mapped_column(String(20), default="", server_default="")
    msg_hook_token: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # --- platform admin: a paused shop cannot sign in (data is kept)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    admin_note: Mapped[str] = mapped_column(String(240), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(190), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(20), default="owner")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Bumped to invalidate every session issued to this user (sign out everywhere, deactivation, password change)
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    phone: Mapped[str] = mapped_column(String(40), default="", server_default="")  # for password-reset codes
    # Forgot-password: a short-lived 6-digit code (stored hashed) sent by SMS/WhatsApp
    reset_code_hash: Mapped[str] = mapped_column(String(256), default="", server_default="")
    reset_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    reset_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (Index("ix_products_business_barcode", "business_id", "barcode"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    sku: Mapped[str] = mapped_column(String(80), default="")
    barcode: Mapped[str] = mapped_column(String(100), default="")
    category: Mapped[str] = mapped_column(String(100), default="General")
    unit: Mapped[str] = mapped_column(String(20), default="pcs", server_default="pcs")
    cost_price: Mapped[Decimal] = mapped_column(Money, default=0)
    selling_price: Mapped[Decimal] = mapped_column(Money, default=0)
    stock_qty: Mapped[int] = mapped_column(Integer, default=0)
    reorder_level: Mapped[int] = mapped_column(Integer, default=5)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, onupdate=utcnow)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(40), default="")
    notes: Mapped[str] = mapped_column(String(240), default="", server_default="")
    credit_limit: Mapped[Decimal] = mapped_column(Money, default=0, server_default="0")
    reminders_sent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    marketing_opt_out: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())  # no broadcasts
    last_reminded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(40), default="")
    notes: Mapped[str] = mapped_column(String(240), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Sale(Base):
    __tablename__ = "sales"
    __table_args__ = (
        UniqueConstraint("business_id", "client_ref", name="uq_sales_business_client_ref"),
        Index("ix_sales_business_created", "business_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id"), nullable=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    reference: Mapped[str] = mapped_column(String(40), unique=True)
    # Idempotency key generated by the client so a retried request never double-records a sale
    client_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    subtotal: Mapped[Decimal] = mapped_column(Money)
    discount: Mapped[Decimal] = mapped_column(Money, default=0)
    total: Mapped[Decimal] = mapped_column(Money)
    amount_paid: Mapped[Decimal] = mapped_column(Money, default=0)
    payment_method: Mapped[str] = mapped_column(String(30), default="cash")
    status: Mapped[str] = mapped_column(String(20), default="completed")  # completed | voided
    void_reason: Mapped[str] = mapped_column(String(240), default="", server_default="")
    voided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    voided_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SaleItem(Base):
    __tablename__ = "sale_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    product_name: Mapped[str] = mapped_column(String(160))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Money)
    unit_cost: Mapped[Decimal] = mapped_column(Money)
    line_total: Mapped[Decimal] = mapped_column(Money)


class Payment(Base):
    """Every money-in event against a sale (initial payment and later debt settlements)."""
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id"), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    method: Mapped[str] = mapped_column(String(30), default="cash")
    # e.g. M-Pesa confirmation code; kept so payments can be reconciled against statements
    external_ref: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    category: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(240), default="")
    amount: Mapped[Decimal] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class StockMovement(Base):
    __tablename__ = "stock_movements"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    # opening | purchase | sale | void | adjustment
    movement_type: Mapped[str] = mapped_column(String(30))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    balance_after: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(String(240), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str] = mapped_column(String(40), default="")
    entity_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class MpesaTransaction(Base):
    """Money that M-Pesa itself confirmed (STK callback or C2B confirmation) — the source of truth for reconciliation."""
    __tablename__ = "mpesa_transactions"
    __table_args__ = (
        UniqueConstraint("business_id", "trans_id", name="uq_mpesa_business_trans"),
        Index("ix_mpesa_business_status", "business_id", "status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    source: Mapped[str] = mapped_column(String(10))  # stk | c2b
    trans_id: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)  # M-Pesa receipt e.g. SKL2AB3CD4
    checkout_request_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    amount: Mapped[Decimal] = mapped_column(Money, default=0)
    phone: Mapped[str] = mapped_column(String(40), default="")
    payer_name: Mapped[str] = mapped_column(String(120), default="")
    bill_ref: Mapped[str] = mapped_column(String(60), default="")
    # pending (STK sent) | failed | unmatched (money in, not linked) | matched (linked to sale/debt) | ignored
    status: Mapped[str] = mapped_column(String(12), default="pending")
    sale_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sales.id"), nullable=True)
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id"), nullable=True)
    matched_by: Mapped[str] = mapped_column(String(20), default="")  # stk | auto_ref | auto_phone | cashier
    result_desc: Mapped[str] = mapped_column(String(240), default="")
    raw: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Shift(Base):
    """A cashier's till session — the basis for end-of-day cash-up and variance tracking."""
    __tablename__ = "shifts"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    opening_float: Mapped[Decimal] = mapped_column(Money, default=0)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expected_cash: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    counted_cash: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    cash_removed: Mapped[Decimal] = mapped_column(Money, default=0)  # banked / taken out during shift
    variance: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    mpesa_total: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    sales_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str] = mapped_column(String(240), default="")
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | closed


class Notification(Base):
    """Outbox for every automated message (WhatsApp / SMS / in-app). dedupe_key makes jobs safe to re-run."""
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("business_id", "dedupe_key", name="uq_notifications_dedupe"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # daily_summary | debt_reminder | low_stock | price_alert | fraud_alert | cash_variance
    channel: Mapped[str] = mapped_column(String(12), default="inapp")  # whatsapp | sms | inapp
    recipient: Mapped[str] = mapped_column(String(60), default="")
    title: Mapped[str] = mapped_column(String(160), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="queued")  # queued | sending | sent | simulated | failed | read
    error: Mapped[str] = mapped_column(String(240), default="")
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    provider_ref: Mapped[Optional[str]] = mapped_column(String(160), nullable=True, index=True)  # WhatsApp wamid / AT messageId
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(10), default="", server_default="")  # own | platform | env


class PurchaseOrder(Base):
    """An order to a supplier. Receiving it adds the stock (with cost) through the normal stock-receiving path."""
    __tablename__ = "purchase_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    reference: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(12), default="draft")  # draft | sent | received | cancelled
    notes: Mapped[str] = mapped_column(String(240), default="", server_default="")
    total: Mapped[Decimal] = mapped_column(Money, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    received_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    product_name: Mapped[str] = mapped_column(String(160))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[Decimal] = mapped_column(Money, default=0)
    received_qty: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class PlatformSetting(Base):
    """Provider-wide settings (e.g. the shared WhatsApp / SMS account). Secret values are stored encrypted."""
    __tablename__ = "platform_settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
