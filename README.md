# Grovia — v0.3

Point of sale, stock, customer credit and plain-language business intelligence for African shops.
FastAPI + SQLAlchemy + PostgreSQL (SQLite for local dev), installable PWA frontend with no build step.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/fatahka-123/duka-intelligence)
![tests](https://github.com/fatahka-123/duka-intelligence/actions/workflows/ci.yml/badge.svg)

**The shop's back office that runs itself.** Most Kenyan POS apps record what happened. Duka checks it, chases it and
explains it: M-Pesa payments reconcile themselves, fake codes are blocked, debts are chased automatically, cash shortages are
traced to a shift, the owner gets a WhatsApp summary every night, and the shop's own records become a lender-ready Duka Score.

See [POSITIONING.md](POSITIONING.md) for how this compares with existing products, [CHANGELOG.md](CHANGELOG.md) for what changed,
and [ROADMAP.md](ROADMAP.md) for what's next.

## What makes it different (v0.3)

| Problem in the market | What Duka does |
|---|---|
| Fake M-Pesa SMS and reused codes at the counter | Every code is checked: reused codes are blocked instantly; with M-Pesa linked, codes M-Pesa never confirmed are flagged to the owner |
| Cashiers reconcile M-Pesa by scrolling SMS | Live M-Pesa feed at the till (tap a payment to use it), STK Push prompts, and Paybill debt repayments that match the right customer automatically |
| Money goes missing and nobody knows when | Shift open/close with blind cash count, expected-vs-counted variance, owner alert, and a per-staff **Loss watch** risk score (voids, discounts, shortages, write-offs, unconfirmed codes) |
| Owners can't be in the shop all day | Nightly WhatsApp/SMS summary in Kiswahili: sales, profit, M-Pesa vs cash, what to restock, debts, fraud flags |
| "Deni" never gets collected | Automatic, polite, bilingual debt reminders that escalate (friendly → overdue → final) in business hours only, then stop |
| Supplier prices rise silently and eat the margin | **Price coach**: when buying price rises, Duka suggests the new selling price that keeps your margin |
| Shops price blindly | Opt-in anonymous **market prices** network: your price vs the median at nearby shops (only with 3+ other shops per barcode) |
| Small shops can't prove their business to lenders | **Duka Score** (0–100) from six transparent factors, indicative affordable loan, and a signed, expiring, revocable **verified statement link** for SACCOs and banks |
| Data entry is the reason shops quit apps | **Quick entry**: type or speak “nimeuza unga 2 na sukari 1 mpesa”, “nimelipa stima 1500”, “Kamau paid 500”, “received 24 milk” — Duka understands, previews, and records it |


## All features

**Selling**
- Fast till: barcode-scanner support (scan → Enter), category chips, editable quantities, F2 to focus scan box
- Cash change calculator, M-Pesa confirmation code capture, discounts, credit and part-paid sales
- Receipts you can print (58/80 mm) or **share on WhatsApp** in one tap
- **Works offline**: sales made without internet are saved on the device and sync automatically; every sale carries an idempotency key so a retry can never double-record it
- Void sales (managers) — stock is returned, refund due is shown, the action is audit-logged

**Stock**
- Receive stock against a supplier with buying price → **weighted-average cost** keeps profit accurate
- Stock counts and write-offs (damaged, expired, theft, own use…) — every change goes through the stock ledger with a running balance
- **Smart restock plan**: sales velocity → days of stock left → suggested order quantity and cost, one tap to send the order to a supplier on WhatsApp
- Slow-mover detection (capital tied up in stock that hasn't sold in 30 days), inventory value at cost and at price

**Customers & credit ("deni")**
- Customer balances, statements, credit limits enforced at the till
- Bilingual (Kiswahili/English) WhatsApp payment reminders

**Intelligence**
- Dashboard "Duka noticed" insights: stock-outs, products priced below cost, items running out, weekly trend, old debts, slow stock, best trading day, expenses exceeding gross profit
- Reports for today / yesterday / week / month / last month / 30 / 90 days with comparison to the previous period, daily revenue chart, top products by profit, payment-method mix, expenses by category, P&L
- **Ask Duka** assistant in English and Kiswahili ("Faida yangu wiki hii?", "Nani anadaiwa?", "What should I restock?") — deterministic, grounded only in your records
- CSV exports (sales, payments, products, expenses) with spreadsheet formula-injection protection

**Team & security**
- Owner / manager / cashier roles. Cashiers can sell and take payments but never see cost prices, profit or reports
- Audit log of sensitive actions, "sign out everywhere", password change revokes other sessions, deactivating a staff member kills their session immediately
- Login throttling (per account and per IP), constant-time responses for unknown emails, PBKDF2-SHA256 600k iterations (old hashes upgraded on login), CSP/HSTS/frame headers, cross-origin write blocking
- Business timezone (default Africa/Nairobi) so "today" means today in the shop, not in UTC

## Run locally

Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m app.seed                   # optional: demo shop with 60 days of history
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. Demo logins (after seeding): `demo@duka.app` / `demo-password-123` (owner) and
`cashier.demo@duka.app` / same password (cashier). API docs at `/docs`.

Locally, SQLite tables are created automatically in `duka.db`.

## Turning on the smart features

**M-Pesa** (Settings → M-Pesa, owner only)
- *Simulator* works immediately with no Safaricom account — use it for demos and staff training.
- **Test first in the sandbox**: on developer.safaricom.co.ke create an app with *M-Pesa Express*, open M-Pesa → Settings,
  tap **Fill sandbox test values** (shortcode 174379 + Safaricom's public test passkey), paste the app's consumer key/secret,
  Save, then **Test connection**. Prompts then reach real Safaricom phones as test payments.
- For real money: complete Safaricom's Go Live for your own Till/Paybill, enter consumer key/secret, your Till or Paybill number,
  the store (head-office) number for tills, and the Lipa na M-Pesa passkey. Set `PUBLIC_BASE_URL`, choose *Sandbox* or *Live*,
  then press **Register C2B URLs**. Secrets are encrypted at rest (`DUKA_ENCRYPTION_KEY`).
- Callbacks are protected by an unguessable per-shop token, de-duplicated on the M-Pesa receipt, and optionally restricted to
  Safaricom IPs (`MPESA_ENFORCE_IPS=true`).

**Automation** (Automation page, owner only) — set the owner's phone, summary time, reminders, price coach and market network.
Without provider keys, messages are generated and visible in the outbox (status *simulated*) but not sent. Add
`WHATSAPP_TOKEN` + `WHATSAPP_PHONE_NUMBER_ID` (and an approved `WHATSAPP_TEMPLATE` for messages outside WhatsApp's 24-hour window)
or Africa's Talking `AT_USERNAME` + `AT_API_KEY`. Jobs run every minute in-process; on multiple instances set
`ENABLE_SCHEDULER=false` and call `POST /internal/cron` with header `X-Cron-Secret`.

**Forgotten passwords** — staff: the owner sets a new one under Staff. Owners: *Forgot password?* on the sign-in page sends a
6-digit code by SMS/WhatsApp to the owner/business phone (needs a provider, see Automation). Server admins can always run
`python -m app.reset_password owner@example.com` (Render → Shell).

**Running Duka for clients (provider admin)** — set `PLATFORM_ADMINS` to your own login email (comma-separate several).
Sign in with that account and an **Admin · all shops** page appears: create a client's shop and owner login (temporary password,
replaced at first sign-in, sendable on WhatsApp), see usage per shop, pause/resume a shop, and reset logins. Everyone else gets 404
on the admin API. Admin actions appear in the client's activity log. Tell clients in writing that the provider can manage their account.

## Tests

```bash
pytest                                                    # isolated temp SQLite DB
DATABASE_URL=postgresql://user@host/testdb AUTO_CREATE_TABLES=false DUKA_SECRET_KEY=... \
  sh -c "alembic upgrade head && pytest"                  # same suite on PostgreSQL
```

40 tests cover tenancy isolation, M-Pesa callbacks/idempotency/auto-matching/fraud guards, shift variance, automation idempotency and quiet hours, Duka Score and lender links, market-price anonymity, quick entry in English and Kiswahili, oversell protection, idempotency, voids, weighted cost, stock adjustments,
credit limits, report arithmetic, insights, the bilingual assistant, roles/permissions, session revocation,
login throttling, legacy password hashes and timezone day boundaries.

## Database migrations (Alembic)

Production schema changes go through Alembic; the Docker image runs `alembic upgrade head` on start.

| Situation | Command |
|---|---|
| New database | `alembic upgrade head` |
| **Existing v0.1 database** (tables made by `create_all`) | `alembic stamp 0001` **once**, then `alembic upgrade head` |
| After changing models | `alembic revision --autogenerate -m "describe change"` and review the file |

Migration `0002` also back-fills one payment record per historical v0.1 sale so payment reports include old data.
The v0.1 → v0.2 upgrade (including down-and-up) was tested on both SQLite and PostgreSQL 16. Back up first.

## Try it in 2 minutes (Render free tier)

1. Click **Deploy to Render** above (sign in with GitHub) and press **Apply**. Render builds the Docker image, creates a free
   PostgreSQL database, runs migrations and starts the app.
2. Open the `https://duka-intelligence-….onrender.com` URL Render shows. Because `SEED_DEMO=true`, a demo shop is created on first
   start: **demo@duka.app / demo-password-123** (owner) and **cashier.demo@duka.app** (cashier, same password).
   Or click *Create an account* to start your own empty shop.
3. Free-tier notes: the app sleeps after ~15 minutes idle (the next visit takes about a minute to wake), automations only run
   while it is awake, and the free database expires after 30 days. Upgrade both plans before real use, and set `SEED_DEMO=false`.

## Deploy (Railway or Render)

1. Push to a private GitHub repository.
2. Railway: new project from the repo (uses `Dockerfile` + `railway.toml`). Render: Blueprint with `render.yaml`.
3. Provision PostgreSQL and set `DATABASE_URL`.
4. Set `DUKA_SECRET_KEY` (≥ 32 random chars), `COOKIE_SECURE=true`, `ENVIRONMENT=production`.
   **The app refuses to start against PostgreSQL with a missing/weak secret.**
5. Verify `/health` (returns 503 if the database is unreachable) and the register/login flow.
6. Configure backups, monitoring, custom domain and HTTPS before onboarding real businesses.

Never commit `.env`, production credentials, customer data or database files.

## Project layout

```
app/
  main.py        app setup, security middleware, error formatting
  config.py      environment settings
  db.py          engine + models
  security.py    hashing, sessions, roles, throttling, audit helper
  schemas.py     request validation + response serializers
  analytics.py   reports, restock/dead-stock/debtor analysis, insights, assistant
  routes/        auth.py · inventory.py · sales.py · business.py · smart.py
  mpesa.py       Daraja client, callbacks, auto-reconciliation, code fraud checks, simulator
  automation.py  scheduler, outbox, WhatsApp/SMS providers, summaries, reminders, alerts
  intelligence.py Duka Score, lender links, loss watch, market prices, price coach, quick-entry parser
  crypto.py      encryption at rest for per-shop secrets
  seed.py        demo data (120 days, M-Pesa traffic, shifts, neighbouring shops)
  static/        index.html · styles.css · app.js · smart.js · sw.js · manifest
migrations/      Alembic environment and versions
tests/           pytest suite
```

## Production gaps — still do not skip

This is a much stronger MVP, but not yet a certified accounting, tax or payment platform. Before public commercial use:

- Independent security review; optional MFA for owners (password reset by SMS/WhatsApp code needs a provider configured)
- Move login throttling to Redis when running more than one instance (it is per-process today)
- Remove `'unsafe-inline'` from the CSP by migrating inline `onclick` handlers to `addEventListener`
- **M-Pesa**: the Daraja integration is built and tested against Safaricom's documented payloads and in simulator mode, but has not
  been run against the live sandbox from this environment. Test end-to-end in the Daraja sandbox before taking real money.
  Safaricom may hash the payer's number in C2B confirmations; then phone-based debt matching falls back to the till feed/manual match.
- **WhatsApp**: business-initiated messages need an approved template outside the 24-hour window; get templates approved before go-live
- **Duka Score** is an indicative business-health measure, not a credit score; any lender partnership needs its own legal review
- **KRA eTIMS** only after official onboarding and certification; receipts are marked "Not a tax invoice"
- VAT handling, proper double-entry ledger
- Offline mode covers selling; product edits, payments and stock receiving still need a connection
- Data Protection Act / ODPC review, privacy policy, terms, retention, data export/deletion procedures
- Automated server-side backups with restore drills (owners can download a JSON backup from Settings), monitoring/alerting, load testing, support process

## Architecture notes

Every business-owned table has `business_id` and every query filters on it — keep this boundary in all new
endpoints and add a two-business isolation test for each. Money is `NUMERIC(14,2)` and handled as `Decimal`.
Timestamps are stored as naive UTC and converted to the business timezone for day boundaries.
Sales lock product rows in a consistent order (`SELECT … FOR UPDATE`) — verified with 8 concurrent tills racing
for 5 units on PostgreSQL: exactly 5 sales succeed and stock ends at 0.
