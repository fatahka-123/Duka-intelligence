# Roadmap — ideas to make Duka Intelligence stand out

> **Shipped in v0.3:** M-Pesa auto-reconciliation & fraud guards (#1), shift cash-up (#2), daily owner summary (#3),
> LLM-free natural-language & voice quick entry (part of #6), area price benchmarks (#8), price coach (#9), Duka Score & verified
> statement (#10). Next up: eTIMS (#4), Android + Bluetooth printing (#5), a WhatsApp bot that uses the quick-entry parser so owners
> can record and query by chatting, and supplier ordering (#11).

Ordered by impact for Kenyan / East African small shops. Each item builds on what v0.2 already has.

## Tier 1 — the features that win shops

### 1. Real M-Pesa integration (Daraja)
- **STK Push at the till**: cashier enters the customer's number, the customer confirms on their phone, the sale marks itself paid. No more checking SMS.
- **Till/Paybill C2B auto-reconciliation**: match incoming payments to sales by amount + time, and flag M-Pesa codes that were typed in but never arrived (common fraud).
- **Debt repayment by Paybill** with the customer's phone as account number → balance updates automatically.
- *Foundation already in place*: `payments` table with `external_ref`, idempotent sales.

### 2. Cash-drawer & shift close ("Funga siku")
- Opening float → expected cash and M-Pesa per cashier → counted cash → variance.
- Variance history per staff member is the single most-requested feature by owners who can't be at the shop all day.

### 3. Daily WhatsApp/SMS summary to the owner
- At closing time: "Leo: mauzo KSh 18,400 · faida KSh 3,100 · M-Pesa 12,300 · 3 items running out · Kamau owes 2,500."
- Owners who work elsewhere and check the shop remotely are a strong paying segment.
- Africa's Talking for SMS; WhatsApp Business Cloud API for rich messages.

### 4. KRA eTIMS compliance
- Integrating eTIMS (after official onboarding and certification) turns Duka from a "nice to have" into a **compliance tool**.
- Pair with VAT/tax categories per product and Z-reports.

### 5. Bluetooth thermal printing + camera scanning
- Android wrapper (Trusted Web Activity or Capacitor) with ESC/POS printing to cheap 58 mm Bluetooth printers.
- Phone camera barcode scanning via the `BarcodeDetector` API, so a shop needs no extra hardware.

## Tier 2 — intelligence that feels like magic

### 6. LLM assistant on top of the existing tools
- Keep `analytics.py` functions as the **only** data source (tool calling) so answers stay grounded and auditable.
- Voice notes in Kiswahili/Sheng ("Nimeuza ngapi leo?"), and "explain this report" summaries.
- Usage limits per plan, and never send raw customer phone numbers to the model.

### 7. Better forecasting
- Seasonality: day-of-week (already detected), month-end salary days, school-opening weeks, holidays (Madaraka, Christmas, Eid).
- Weather-sensitive items (umbrellas, cold drinks) via a free weather API.

### 8. Area price benchmarks (opt-in, anonymised)
- "Your 2 kg Jogoo is KSh 10 above the median for shops near you" / "You could raise sugar by KSh 5 and stay below the area average."
- Becomes a network effect: the more shops join, the smarter every shop gets.

### 9. Margin and pricing coach
- Suggest prices from a target margin; warn when supplier price rises (detected at stock receiving) but selling price didn't change.

## Tier 3 — growth and revenue lines

### 10. Lending readiness ("Duka Score")
- A verified sales and repayment history is exactly what SACCOs, banks and stock-financing lenders lack for informal retail.
- Shareable, owner-consented business statement PDF → partner referral revenue.

### 11. Supplier ordering & group buying
- Restock plan → one-tap purchase order to the distributor → delivery confirmation updates stock.
- Aggregate orders across shops in an area for better distributor prices.

### 12. Customer loyalty by phone number
- Points or "buy 10 get 1", birthday SMS, win-back messages to customers who stopped visiting (data already exists in statements).

### 13. Multi-branch
- Branch-level stock, transfers between branches, consolidated owner dashboard.

### 14. Vertical editions
- **Pharmacy / agrovet**: batch and expiry tracking, expiry alerts, controlled-item logs.
- **Hardware**: units and conversions (bag ↔ kg, roll ↔ metre).
- **Butchery / greengrocer**: weight-based sales and scales.
- **Salon / services**: services + commission per stylist.

## Engineering foundations to schedule alongside
- Password reset (SMS OTP), optional MFA for owners
- Redis-backed rate limiting and a job queue (daily summaries, reminders)
- Full offline-first data layer (IndexedDB) for stock receiving and payments, not just sales
- Partial returns/refunds; double-entry ledger; VAT
- Remove inline handlers → strict CSP without `'unsafe-inline'`
- Observability (Sentry, structured logs), automated backups with restore drills
- ODPC data-protection compliance: consent for marketing messages, export & deletion tools

## Suggested pricing to test
| Plan | Price | Includes |
|---|---|---|
| Bure (Free) | KSh 0 | 1 user, 100 products, reports for the last 7 days |
| Duka | ~KSh 499/month | 3 users, unlimited products, insights, WhatsApp receipts |
| Duka Pro | ~KSh 1,499/month | M-Pesa STK, daily owner summary, shift close, eTIMS, multi-branch |

Collect subscriptions through M-Pesa itself — the lowest-friction payment for this market.
