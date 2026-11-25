"""M-Pesa (Safaricom Daraja) integration and the auto-reconciliation engine.

Modes per business:
  off        – M-Pesa is only a manually recorded payment method (v0.2 behaviour)
  simulator  – no Safaricom account needed; prompts and till payments are simulated for demos/training
  sandbox    – Daraja sandbox (https://sandbox.safaricom.co.ke)
  production – live Daraja (https://api.safaricom.co.ke)

Daraja does not sign callbacks, so each business gets an unguessable callback token in the URL,
callbacks are parsed defensively, de-duplicated on TransID / CheckoutRequestID, and money is only
ever applied from M-Pesa-confirmed data — never from what a cashier typed.
"""
from __future__ import annotations

import base64
import json
import re
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .crypto import decrypt
from .db import Business, Customer, MpesaTransaction, Notification, Payment, Sale, utcnow

NAIROBI = ZoneInfo("Africa/Nairobi")
# Public test values published by Safaricom for the Daraja sandbox (M-Pesa Express / STK Push)
SANDBOX_SHORTCODE = "174379"
SANDBOX_PASSKEY = "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919"
BASE_URLS = {"sandbox": "https://sandbox.safaricom.co.ke", "production": "https://api.safaricom.co.ke"}
# Safaricom-published callback source IPs. Enforced when MPESA_ENFORCE_IPS=true (behind a proxy, make sure X-Forwarded-For is trustworthy).
SAFARICOM_IPS = {"196.201.214.200", "196.201.214.206", "196.201.213.114", "196.201.214.207", "196.201.214.208",
                 "196.201.213.44", "196.201.212.127", "196.201.212.138", "196.201.212.129", "196.201.212.136",
                 "196.201.212.74", "196.201.212.69"}


class MpesaError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def normalize_phone(phone: str) -> str:
    """07XX / 01XX / +2547XX / 2547XX → 2547XXXXXXXX. Returns '' if it doesn't look Kenyan."""
    p = re.sub(r"\D", "", phone or "")
    if p.startswith("0") and len(p) == 10:
        p = "254" + p[1:]
    elif len(p) == 9 and p[0] in "17":
        p = "254" + p
    return p if re.fullmatch(r"254[17]\d{8}", p) else ""


def mask_phone(phone: str) -> str:
    p = normalize_phone(phone) or phone
    return p[:6] + "***" + p[-3:] if len(p) >= 10 else p


def ensure_callback_token(b: Business) -> str:
    if not b.mpesa_callback_token:
        b.mpesa_callback_token = secrets.token_urlsafe(24)
    return b.mpesa_callback_token


def mpesa_active(b: Business) -> bool:
    return b.mpesa_mode in ("simulator", "sandbox", "production")


# ---------------------------------------------------------------- Daraja client
class Daraja:
    def __init__(self, b: Business, public_base_url: str):
        if b.mpesa_mode not in BASE_URLS:
            raise MpesaError("Daraja is not configured for this business")
        self.b = b
        self.base = BASE_URLS[b.mpesa_mode]
        self.key, self.secret, self.passkey = decrypt(b.mpesa_consumer_key), decrypt(b.mpesa_consumer_secret), decrypt(b.mpesa_passkey)
        if not (self.key and self.secret and b.mpesa_shortcode):
            raise MpesaError("Add your Daraja consumer key, secret and shortcode in Settings → M-Pesa")
        self.callback_base = public_base_url.rstrip("/") + f"/api/mpesa/callback/{ensure_callback_token(b)}"

    def _token(self) -> str:
        try:
            r = httpx.get(f"{self.base}/oauth/v1/generate", params={"grant_type": "client_credentials"},
                          auth=(self.key, self.secret), timeout=15)
        except httpx.HTTPError:
            raise MpesaError("Could not reach Safaricom — check the server's internet connection and try again")
        if r.status_code != 200:
            raise MpesaError(f"Safaricom rejected the consumer key/secret ({r.status_code}). Check they are for the "
                             f"{'sandbox' if self.b.mpesa_mode == 'sandbox' else 'live'} app on developer.safaricom.co.ke")
        return r.json()["access_token"]

    def test_connection(self) -> dict:
        """Checks the keys by requesting an access token. Sends nothing to any phone."""
        self._token()
        problems = []
        if not self.passkey:
            problems.append("No passkey yet — payment prompts (STK Push) won't work until you add it")
        if self.b.mpesa_mode == "production" and self.b.mpesa_shortcode == SANDBOX_SHORTCODE:
            problems.append("174379 is the sandbox test shortcode — use your own Till/Paybill for live mode")
        return {"ok": True, "message": "Connected to Safaricom ✓ — your keys work", "warnings": problems,
                "callback": self.callback_base}

    def stk_push(self, phone: str, amount: int, account_ref: str, description: str) -> dict:
        if not self.passkey:
            raise MpesaError("STK Push needs the Lipa na M-Pesa passkey")
        ts = datetime.now(NAIROBI).strftime("%Y%m%d%H%M%S")  # Daraja expects East Africa Time
        # For Buy Goods tills, the password/BusinessShortCode use the store (head office) number and PartyB is the till
        business_code = self.b.mpesa_store_number or self.b.mpesa_shortcode
        password = base64.b64encode(f"{business_code}{self.passkey}{ts}".encode()).decode()
        body = {
            "BusinessShortCode": business_code, "Password": password, "Timestamp": ts,
            "TransactionType": "CustomerBuyGoodsOnline" if self.b.mpesa_type == "till" else "CustomerPayBillOnline",
            "Amount": int(amount), "PartyA": phone, "PartyB": self.b.mpesa_shortcode, "PhoneNumber": phone,
            "CallBackURL": f"{self.callback_base}/stk", "AccountReference": account_ref[:12], "TransactionDesc": description[:13],
        }
        token = self._token()
        try:
            r = httpx.post(f"{self.base}/mpesa/stkpush/v1/processrequest", json=body,
                           headers={"Authorization": f"Bearer {token}"}, timeout=20)
        except httpx.HTTPError:
            raise MpesaError("Safaricom did not respond — try the prompt again")
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or str(data.get("ResponseCode")) != "0":
            raise MpesaError(data.get("errorMessage") or data.get("ResponseDescription") or f"STK Push failed ({r.status_code})")
        return data

    def register_c2b_urls(self) -> dict:
        body = {"ShortCode": self.b.mpesa_shortcode, "ResponseType": "Completed",
                "ConfirmationURL": f"{self.callback_base}/c2b/confirmation", "ValidationURL": f"{self.callback_base}/c2b/validation"}
        r = httpx.post(f"{self.base}/mpesa/c2b/v1/registerurl", json=body, headers={"Authorization": f"Bearer {self._token()}"}, timeout=20)
        if r.status_code != 200:
            raise MpesaError(f"Register URL failed: {r.text[:200]}")
        return r.json()


# ---------------------------------------------------------------- applying money
def apply_to_sale(db: Session, sale: Sale, tx: MpesaTransaction, amount: Decimal, matched_by: str, user_id: int | None = None) -> Decimal:
    """Apply up to ``amount`` to the sale's balance. Returns the amount applied."""
    balance = sale.total - sale.amount_paid
    use = min(balance, amount)
    if use <= 0:
        return Decimal("0")
    sale.amount_paid += use
    db.add(Payment(business_id=sale.business_id, sale_id=sale.id, user_id=user_id, amount=use, method="mpesa",
                   external_ref=tx.trans_id or ""))
    return use


def apply_to_customer_debts(db: Session, customer: Customer, tx: MpesaTransaction, matched_by: str) -> Decimal:
    """Settle a customer's oldest unpaid sales first (FIFO)."""
    remaining = tx.amount
    open_sales = db.scalars(select(Sale).where(Sale.business_id == customer.business_id, Sale.customer_id == customer.id,
                                               Sale.status == "completed", Sale.total > Sale.amount_paid)
                            .order_by(Sale.created_at).with_for_update())
    for s in open_sales:
        if remaining <= 0:
            break
        remaining -= apply_to_sale(db, s, tx, remaining, matched_by)
    return tx.amount - remaining


def auto_match(db: Session, b: Business, tx: MpesaTransaction) -> None:
    """Try to link an incoming C2B payment without human help.

    1. Bill reference = a sale reference (Paybill customers paying a specific receipt)
    2. Bill reference or phone = a customer's phone with an outstanding balance (debt repayment)
    Otherwise it stays 'unmatched' and appears in the live feed at the till for one-tap attachment.
    """
    ref = (tx.bill_ref or "").strip().upper()
    if ref:
        sale = db.scalar(select(Sale).where(Sale.business_id == b.id, func.upper(Sale.reference) == ref, Sale.status == "completed").with_for_update())
        if sale and sale.total > sale.amount_paid:
            applied = apply_to_sale(db, sale, tx, tx.amount, "auto_ref")
            tx.status, tx.sale_id, tx.customer_id, tx.matched_by = "matched", sale.id, sale.customer_id, "auto_ref"
            if applied < tx.amount:
                tx.result_desc = f"Overpaid by KSh {tx.amount - applied:,.2f}"
            return
    for candidate in filter(None, {normalize_phone(ref), normalize_phone(tx.phone)}):
        for c in db.scalars(select(Customer).where(Customer.business_id == b.id)):
            if normalize_phone(c.phone) == candidate:
                applied = apply_to_customer_debts(db, c, tx, "auto_phone")
                if applied > 0:
                    tx.status, tx.customer_id, tx.matched_by = "matched", c.id, "auto_phone"
                    if applied < tx.amount:
                        tx.result_desc = f"KSh {tx.amount - applied:,.2f} more than the debt"
                    db.add(Notification(business_id=b.id, kind="payment_received", channel="inapp", status="sent", sent_at=utcnow(),
                                        title=f"{c.name} paid KSh {tx.amount:,.0f} by M-Pesa",
                                        body=f"Automatically applied to their balance ({tx.trans_id})."))
                    return
    tx.status = "unmatched"


# ---------------------------------------------------------------- callbacks
def parse_stk_callback(payload: dict) -> dict:
    cb = payload["Body"]["stkCallback"]
    items = {i.get("Name"): i.get("Value") for i in (cb.get("CallbackMetadata") or {}).get("Item", []) if isinstance(i, dict)}
    return {"checkout_request_id": str(cb["CheckoutRequestID"]), "result_code": int(cb.get("ResultCode", 1)),
            "result_desc": str(cb.get("ResultDesc", ""))[:240], "amount": Decimal(str(items.get("Amount", 0) or 0)),
            "receipt": str(items.get("MpesaReceiptNumber") or ""), "phone": str(items.get("PhoneNumber") or "")}


def handle_stk_callback(db: Session, b: Business, payload: dict) -> None:
    data = parse_stk_callback(payload)
    tx = db.scalar(select(MpesaTransaction).where(MpesaTransaction.business_id == b.id,
                                                  MpesaTransaction.checkout_request_id == data["checkout_request_id"]).with_for_update())
    if not tx or tx.status != "pending":
        return  # unknown or already processed (Daraja retries callbacks)
    tx.raw = json.dumps(payload)[:4000]
    tx.result_desc = data["result_desc"]
    if data["result_code"] != 0:
        tx.status = "failed"
        return
    if data["receipt"] and db.scalar(select(MpesaTransaction.id).where(MpesaTransaction.business_id == b.id,
                                                                      MpesaTransaction.trans_id == data["receipt"].strip().upper())):
        tx.status = "ignored"  # receipt already recorded via C2B
        return
    receipt = data["receipt"].strip().upper()
    if receipt and not re.fullmatch(r"[A-Z0-9]{6,20}", receipt):
        tx.status, tx.result_desc = "failed", "Malformed receipt in callback"
        return
    tx.trans_id, tx.amount = receipt or None, data["amount"] or tx.amount
    tx.phone = mask_phone(data["phone"]) or tx.phone
    sale = db.get(Sale, tx.sale_id) if tx.sale_id else None
    if sale and sale.business_id == b.id and sale.status == "completed":  # prompt sent for an existing debt/sale
        apply_to_sale(db, sale, tx, tx.amount, "stk")
        tx.status, tx.matched_by = "matched", "stk"
    else:
        # Prompt sent from the till before the sale exists: the till is polling and will complete
        # the sale with this confirmed receipt. Until then it is money-in waiting to be linked.
        tx.status = "unmatched"


def handle_c2b_confirmation(db: Session, b: Business, payload: dict) -> MpesaTransaction | None:
    trans_id = str(payload.get("TransID") or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6,20}", trans_id):
        return None  # M-Pesa receipts are alphanumeric; anything else is not a genuine confirmation
    if db.scalar(select(MpesaTransaction).where(MpesaTransaction.business_id == b.id, MpesaTransaction.trans_id == trans_id)):
        return None  # duplicate delivery
    name = " ".join(str(payload.get(k) or "").strip() for k in ("FirstName", "MiddleName", "LastName")).strip()
    tx = MpesaTransaction(business_id=b.id, source="c2b", trans_id=trans_id, amount=Decimal(str(payload.get("TransAmount") or 0)),
                          phone=mask_phone(str(payload.get("MSISDN") or "")), payer_name=name[:120],
                          bill_ref=str(payload.get("BillRefNumber") or "")[:60], raw=json.dumps(payload)[:4000])
    # keep the full number only for matching (Safaricom may send it hashed — then phone matching simply won't fire)
    full_phone = normalize_phone(str(payload.get("MSISDN") or ""))
    db.add(tx)
    db.flush()
    if full_phone:
        tx.phone = full_phone
        auto_match(db, b, tx)
        tx.phone = mask_phone(full_phone)
    else:
        auto_match(db, b, tx)
    return tx


# ---------------------------------------------------------------- simulator (demo / training mode)
def simulate_c2b(db: Session, b: Business, amount: Decimal, phone: str = "", name: str = "", bill_ref: str = "") -> MpesaTransaction:
    code = "S" + secrets.token_hex(5).upper()[:9]
    return handle_c2b_confirmation(db, b, {"TransactionType": "Pay Bill" if b.mpesa_type == "paybill" else "Buy Goods",
                                           "TransID": code, "TransAmount": str(amount), "MSISDN": phone or "254712345678",
                                           "FirstName": name or "SIMULATED", "BillRefNumber": bill_ref})


def simulate_stk_result(db: Session, b: Business, tx: MpesaTransaction, success: bool = True) -> None:
    items = [{"Name": "Amount", "Value": float(tx.amount)}, {"Name": "MpesaReceiptNumber", "Value": "S" + secrets.token_hex(5).upper()[:9]},
             {"Name": "PhoneNumber", "Value": tx.phone}]
    handle_stk_callback(db, b, {"Body": {"stkCallback": {
        "MerchantRequestID": "sim", "CheckoutRequestID": tx.checkout_request_id,
        "ResultCode": 0 if success else 1032, "ResultDesc": "The service request is processed successfully." if success else "Request cancelled by user",
        "CallbackMetadata": {"Item": items} if success else None}}})


# ---------------------------------------------------------------- fraud checks on manually typed codes
CODE_RE = re.compile(r"^[A-Z0-9]{8,12}$")


def check_manual_code(db: Session, b: Business, code: str, amount: Decimal) -> dict:
    """Verify an M-Pesa code typed by a cashier.

    - reused: the same code was already used on another payment (the most common till fraud)
    - verified: M-Pesa confirmed this code for this business, with a matching amount
    - unverified: integration is on but M-Pesa has not confirmed the code (yet)
    """
    code = (code or "").strip().upper()
    if not code:
        return {"status": "missing"}
    if not CODE_RE.match(code):
        return {"status": "invalid", "message": "M-Pesa codes are 10 letters/numbers, e.g. SKL2AB3CD4"}
    reused = db.scalar(select(func.count(Payment.id)).where(Payment.business_id == b.id, func.upper(Payment.external_ref) == code))
    if reused:
        return {"status": "reused", "message": f"Code {code} was already used for another payment"}
    if not mpesa_active(b):
        return {"status": "not_checked"}
    tx = db.scalar(select(MpesaTransaction).where(MpesaTransaction.business_id == b.id, MpesaTransaction.trans_id == code))
    if not tx:
        return {"status": "unverified", "message": f"M-Pesa has not confirmed {code} for this till"}
    if tx.status == "matched":
        return {"status": "reused", "message": f"{code} is already linked to another sale"}
    if tx.amount < amount:
        return {"status": "short", "message": f"{code} was for KSh {tx.amount:,.2f}, less than KSh {amount:,.2f}", "tx_id": tx.id}
    return {"status": "verified", "tx_id": tx.id}


def stale_unverified_codes(db: Session, b: Business, older_than_minutes: int = 15) -> list[Payment]:
    """Manually typed M-Pesa payments that M-Pesa never confirmed (integration must be on)."""
    if not mpesa_active(b):
        return []
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    since = utcnow() - timedelta(days=7)
    confirmed = select(MpesaTransaction.trans_id).where(MpesaTransaction.business_id == b.id, MpesaTransaction.trans_id.is_not(None))
    return list(db.scalars(select(Payment).join(Sale, Sale.id == Payment.sale_id).where(
        Payment.business_id == b.id, Payment.method == "mpesa", Payment.external_ref != "", Sale.status == "completed",
        Payment.created_at < cutoff, Payment.created_at >= since, Payment.external_ref.not_in(confirmed))))
