# Changelog

## 0.7.0 — WhatsApp & SMS built in
- **Messaging page**: connect WhatsApp Cloud API and Africa's Talking from inside the app (keys encrypted), with step-by-step
  guides, *Test connection* (shows the SMS balance and the verified WhatsApp number) and *Send me a test*
- **Shared account for client shops**: the provider sets one WhatsApp/SMS account under Admin → Shared messaging; each shop uses it,
  its own keys, or turns messaging off. The provider can switch the shared account off per shop and sees SMS/WhatsApp usage per shop.
  Server environment variables still work as a last fallback
- **Automatic SMS backup** when a WhatsApp message fails (e.g. outside the 24-hour window without a template)
- **Delivery tracking**: WhatsApp webhooks (signed with the app secret when set) and Africa's Talking delivery reports mark messages
  delivered / read / failed; each shop's webhook can only update that shop's messages. *Retry* for failed or simulated messages
- **Broadcasts**: send an offer or notice to all customers, debtors, inactive (30+ days) or recent customers, with {name}
  personalisation, live preview, SMS segment counter, and a per-customer "no promotional messages" opt-out
- **Send receipts by SMS/WhatsApp** straight from Grovia (the old "share via my WhatsApp" link remains)

## 0.6.0 — colour themes, modern look, deleting client shops
- **Six colour themes** — Forest, Ocean, Sunset, Royal, Rose, Graphite — each in Light, Dark or *Same as device*
  (Settings → Appearance, saved per device, applied before first paint). Dark mode is tinted by the chosen theme
- **Modern design layer**: gradient sidebar with an accent rail, frosted top bar, lead metric card, soft-shadow cards that lift
  on hover, gradient buttons, larger rounded inputs with focus rings, page/modal/toast motion (off when the device asks for
  reduced motion), refreshed sign-in page, system-font fallbacks so headings never fall back to a serif
- **Contract ended → delete a client shop** (provider admin only): download that shop's full backup, pause it, then delete it
  permanently by typing its exact name and a reason. Every record of the shop is erased; a summary (no customer data)
  stays in the provider's own activity log. The provider's own shop and other admins' shops can't be deleted

## 0.5.1 — renamed to Grovia
- The app is now **Grovia** everywhere users see it: sign-in page, sidebar, browser tab, installed-app name and icon,
  *Ask Grovia*, *Grovia Score*, verified statements, WhatsApp/SMS texts. Internal names (env vars, cookies, saved settings)
  are unchanged, so nobody is signed out and no configuration changes

## 0.5.0 — provider admin & client onboarding
- **Admin · all shops** page for the people listed in `PLATFORM_ADMINS` (invisible — 404 — to everyone else): every client shop
  with owner, staff count, 30-day sales, last activity and M-Pesa mode; search; totals across shops
- **New client**: creates a separate shop and owner login with a readable temporary password, shown once, with Copy and
  *Send on WhatsApp* (website, email and password in one message)
- **Pause / resume a shop**: paused shops can't sign in and open sessions stop working at once; data is kept. Private notes per shop
- **Reset any login** to a new temporary password (signs that person out everywhere)
- **Temporary passwords must be replaced at first sign-in** — for client owners, staff added by an owner, and admin resets;
  the new password must differ from the old one
- Every admin action is written to that shop's activity log, marked "by provider"

## 0.4.1 — dark mode
- **Appearance setting** (Settings → Appearance): Light, Dark or *Same as device*. Saved per device, applied before the page
  paints (no white flash), follows the phone's setting live in *Same as device*; receipts stay paper-white for printing

## 0.4.0 — returns, purchase orders and real M-Pesa setup

### Added
- **Returns & refunds**: return part (or all) of a sale from the receipt. Stock goes back (or is written off for damaged goods),
  the sale and every report are reduced, money paid above the new total is refunded as a negative payment that also lowers
  the cashier's expected cash at cash-up; credit sales just owe less. Shown on the receipt and in the audit log
- **Purchase orders**: build an order by hand or straight from the smart restock plan, send it to the supplier on WhatsApp,
  then receive the delivery (full or partial, with actual prices) into stock in one tap — weighted cost and price-coach alerts apply
- **Forgot password**: 6-digit code by SMS/WhatsApp (hashed, 15-minute expiry, 5 tries, signs out other devices); staff phone numbers;
  `python -m app.reset_password EMAIL` for server admins
- **Full backup**: owners can download every record as one JSON file from Settings (passwords and API keys excluded)
- **M-Pesa setup**: step-by-step guide, one-tap Safaricom sandbox test values, *Test connection* button with clear errors,
  and the till now says plainly when it is in simulator mode (no real phone prompt)

### Fixed
- Daraja STK timestamp now uses East Africa Time; network failures to Safaricom give a readable message instead of a server error

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
