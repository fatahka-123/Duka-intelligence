# Changelog

## 0.3.0 — "the back office that runs itself"

### Added
- **M-Pesa auto-reconciliation (Daraja)**: STK Push from the till (sale completes when M-Pesa confirms), C2B confirmation
  callbacks, live payment feed at the till, automatic matching of Paybill repayments to sales (by receipt) or customer debts
  (by phone, oldest debt first), manual match/dismiss, simulator mode, per-shop encrypted credentials, token-protected and
  idempotent callbacks, optional Safaricom IP allow-list
- **M-Pesa fraud guards**: reused codes rejected everywhere; with M-Pesa linked, codes are verified against confirmed payments,
  short payments rejected, and unconfirmed codes flagged to the owner after 15 minutes
- **Shifts & cash-up**: open with float, blind count at close, expected vs counted variance, owner alert on shortages
- **Loss watch**: per-staff risk from voids, unusual discounts, cash shortages, stock write-offs and unconfirmed codes; shrinkage %
- **Automation engine**: nightly owner summary (WhatsApp/SMS), escalating bilingual debt reminders (business hours, weekly, max 3),
  daily stock-out alerts, fraud and variance alerts; idempotent outbox, atomic dispatch, WhatsApp Cloud API and Africa's Talking
  providers, simulated mode, in-app alert centre with unread badge, `/internal/cron` for multi-instance deployments
- **Price coach**: buying-price increases trigger a suggested selling price that keeps the margin
- **Market prices network**: opt-in, anonymous medians by barcode with a minimum of 3 other shops, county-level when possible
- **Duka Score**: six weighted, explained factors; indicative affordable loan; tips; 6-month statement;
  signed, expiring, revocable **verified statement link** for lenders
- **Quick entry**: natural-language bookkeeping in English and Kiswahili (sales, expenses, stock received, customer payments),
  with preview, editable quantities, one-tap product alternatives, and voice input where the browser supports it
- Richer demo seed (120 days, M-Pesa traffic, shifts with a shortage pattern, fake codes, neighbouring shops)
- Migration `0003`

### Fixed
- Verified statement no longer shows months before the shop joined or averages in a partial first month
- Quick-entry confirmation no longer races with the dashboard refresh
- Sidebar scrolls on short screens


## 0.2.0

### Bugs fixed (found in v0.1)
- **Stock could go negative**: repeating a product as two cart lines bypassed the stock check (3 in stock, sold 6 → −3). Lines are now merged before checking.
- **Editing a product overwrote stock silently** (no stock movement, no audit). Stock now only changes through receive / adjust / sale / void.
- **"Today" was UTC midnight**, so in Kenya sales between 00:00 and 03:00 landed on the wrong day. Day boundaries now use the business timezone.
- Any text was accepted as a payment method; POS "credit" was not accepted by the payment endpoint. Methods are validated consistently.
- Customer balances included voided sales; credit could be given to anonymous walk-ins with no way to follow up.
- Validation errors reached the UI as `[object Object]`. They now return one readable sentence.
- Dashboard and reports ran one query per sale (N+1); now aggregated in SQL.
- Tests wrote into your real `duka.db`; they now use an isolated temp database.
- `datetime.utcnow()` and `on_event` deprecations removed.

### Security
- Login throttling (5 failures per account / 30 per IP per 15 min), timing-safe unknown-email handling
- PBKDF2 raised to 600k iterations with transparent upgrade of v0.1 hashes
- Session revocation: sign out everywhere, password change, staff deactivation/role change
- Cross-origin write blocking, Content-Security-Policy, HSTS (when `COOKIE_SECURE=true`)
- App refuses to start on PostgreSQL / production with the default or a short secret
- CSV exports neutralise spreadsheet formulas; search escapes SQL `LIKE` wildcards
- Container runs as a non-root user

### Added
- Staff accounts with owner / manager / cashier roles; audit log
- Payments ledger (every payment, method and M-Pesa code), void with stock return
- Stock receiving with supplier and weighted-average cost; stock counts and write-offs with reasons; stock history
- Barcode / SKU lookup, unique SKU/barcode per shop, units of measure
- Customer statements, credit limits, debtor list, WhatsApp reminders
- Idempotent sales (`client_ref`) and an offline sale queue in the PWA
- Reports for any period with previous-period comparison, daily series, top products by profit, payment mix, expense categories
- Insights engine and restock planner (sales velocity, days of cover, suggested order)
- Bilingual assistant (English/Kiswahili) with period understanding ("today", "wiki hii", "last 7 days")
- Business settings (address, KRA PIN, receipt footer, timezone); printable/WhatsApp receipts
- CSV exports; `/health` checks the database
- Alembic migrations with a v0.1 baseline and data back-fill; demo data seeder
- Readable, modular code (backend split into modules; frontend un-minified)
