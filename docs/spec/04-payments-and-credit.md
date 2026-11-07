# 4. Payments and member credit

## 4.1 Concepts

- **Payment** = one ledger entry: *what for* (`reason`), *paid how* (`source`), *how much*, *status*.
- **Member credit** ("BB Credit", `cash_balance`) = money a member holds with the space, spendable on fees.
- **Subscription charge** = a month's subscription that is owed (see 03 §3.3).

### Reasons and fixed prices

| Reason | Amount | Effect when recorded |
|---|---|---|
| `subscription` | member's `monthly_subscription` | Linked to the oldest pending/due charge → charge `processing` (if pending) or `paid`; extends expiry. |
| `induction` | equipment `access_fee` (whole £) | Creates an **Induction** record (`paid = true`) for that equipment's `induction_category`. |
| `key-deposit` (legacy `door-key`) | **£15** (a setting) | Deposit for the physical key that gives 24-hour access; needed before an admin grants key holder (05 §5.2). Fobs are free. |
| `storage-box` | the box fee (a setting, £15) | One-off, non-refundable; sets the member's "box paid" flag (06 §6.2). Legacy £5 deposits stay as history. |
| `locker-rent` | the locker's monthly rent (size default, or an admin override) | Pays a locker rent charge (06 §6.2). Billed monthly with the subscription. |
| `balance` | any | Adds to member credit (top-up). |
| `equipment-fee` | computed | Taken from credit when a session ends (05 §5.4, §5.6). If an admin later adjusts the session's times, a correcting `equipment-fee` entry (a refund or an extra charge) is added; the original is never edited. |
| `withdrawal` | any | Credit paid out (cash) by an admin. |
| `donation` | any | Legacy only: unmatched PayPal money. Kept in migrated history; can be refunded to a member's credit. |
| `costs`, consumable service names | any | Deducted from credit. |

**Decided:** the key deposit and box fee are admin-editable **settings** (02 §2.8) and locker rents come from the locker size list (06 §6.2), not code. The amount charged is always worked out **on the server** from the reason and these settings, never taken from the browser.

### Sources

| Source | How it's created |
|---|---|
| `gocardless-variable` | App creates a GoCardless *payment* against the member's mandate (subscription billing, or member clicks "pay by DD"). Status starts `pending`, updated by webhook. |
| `stripe` | Card payment via Stripe Checkout/token; recorded `paid` immediately with fee = 2.4% + 20p (estimated, hard-coded). |
| `balance` | Spent from member credit. |
| `cash` | Admin records cash received. |
| `standing-order` | Bank statement import (finance). |
| `manual` | Admin-recorded (e.g. induction paid outside the system). |
| `reimbursement` | Approved expense credited to member. |

### Where a member can pay what (legacy "payment module")

| Payment for | Allowed methods |
|---|---|
| Subscription (when there's a problem) | GoCardless, Stripe, credit |
| Induction fee | GoCardless, credit, Stripe |
| Key deposit | GoCardless, Stripe, credit |
| Box fee | GoCardless, credit, Stripe |
| Locker rent (monthly) | GoCardless (Direct Debit mandate), credit |
| Credit top-up | GoCardless, Stripe |
| Equipment fees | credit |

**Paying from credit** is refused if it would take credit below the allowed minimum: **£0** for everything,
except **equipment fees, which may take credit down to −£5**.

### Build status (2026-11-07)

Built in `apps/payments`: the ledger (`Payment`), credit worked out from it, paying from credit with its minimum, the
machine-fee charging and corrections, cash and manual payments in the admin ("Record a payment"), the member's credit
page with withdrawal requests, and finance's handling of those requests. Choices made while building:

- **Amounts are signed.** A correction that gives money back is a new row with a negative amount, linked to the row it
  corrects; the credit rule above handles it without special cases. Rows are read-only in the admin.
- **Machine fees go through one function**, `settle_session`: when a session ends, and again after its times are
  adjusted, it's reclassified, or the tidy-up merges it with the next one. It posts only the difference, so running
  it twice changes nothing.
- The hourly tidy-up only merges sessions from the **last two days**, now that sessions are charged straight away.
- Sessions the **legacy system billed** are history: adjusting one moves no money (the admin says so).
- The v1 device API now sends the real credit as `cash_balance` (pence), so the laser's screen shows it.
- Consumables use the reason `consumable`, with the service in the reference.
- Withdrawal requests keep the bank details only until finance marks the request paid or rejected.

Still to build: GoCardless, Stripe, subscription charges and billing (03), the statement import (4.6), the finance
overview and money summary (4.8, 4.9), member-facing top-ups and paying fees from the credit page, and importing the
legacy payments (08 §8.2). Prices are constants in `apps/payments/prices.py` until the settings app exists.

## 4.2 GoCardless (Direct Debit)

**Set-up:** redirect flow pre-filled with name, email and address → member authorises → return URL confirms
the flow → mandate id stored → payment method `gocardless-variable` → activate (03 §3.4).

**Billing:** the app creates a GoCardless payment (amount in pence, description per reason) against the mandate.
`pending_submission` is stored as `pending`.

**Webhook** (`POST /gocardless/webhook`): HMAC-SHA256 of the raw body with the webhook secret must equal the
`Webhook-Signature` header (otherwise 403). Events handled:

| Resource / action | Effect |
|---|---|
| `payments.created` | Logged only. |
| `payments.submitted` | Matching payment → `pending`. |
| `payments.confirmed` | Matching payment → `paid` (`paid_at = now`); if it's a subscription, its charge → `paid`, expiry = payment date + 1 month. |
| `payments.paid_out` | Ignored. |
| `payments.failed` / `cancelled` | Payment takes the provider's status. The charge stays `processing` while a retry is due (by GoCardless, or by BBMS after a set number of days, depending on the retry setting) and the member is emailed; when it has finally failed, the charge becomes `failed` (03 §3.6). BBMS never re-bills it nightly. Expiry resets to the last paid charge + 1 month (or today). |
| `payments.resubmission_requested` | GoCardless is retrying: the payment goes back to `pending`. |
| `mandates.cancelled` | Member → `leaving`; DD details cleared. |
| anything else (e.g. `surcharge_fee_debited`) | Logged. |

**New requirements:** constant-time signature comparison; store each webhook event id and **ignore duplicates**;
process asynchronously but in order; record failures for a human to review; retry failed Direct Debits only as the retry setting says, never nightly (03 §3.6).

## 4.3 Stripe (cards)

Member enters a card (Stripe Elements/Checkout) for a given reason and amount → charge created in GBP → payment
recorded `paid` with estimated fee.

**New:** use **Stripe Checkout + webhook** (`checkout.session.completed`) instead of trusting a client-side amount;
compute the amount server-side from the reason; record the real fee from the balance transaction.

## 4.4 PayPal — dropped

Not rebuilt (decided 2026-11-05). Historic PayPal payments are migrated as ledger history. Before cutover, check
whether any member still pays by PayPal and move them to Direct Debit or standing order (08).

## 4.5 Cash & manual (admin)

- **Record cash** for a member with any reason.
- **Cash withdrawal** from a member's credit (refused if insufficient).
- **Manual payment** form: subscription (extends membership 1 month from today), induction (creates induction record),
  key deposit (at the price in settings), a box fee (at the price in settings, e.g. paid in cash), credit top-up (any amount).

## 4.6 Bank statement import (finance)

Upload a bank-statement spreadsheet; **test mode** previews without saving.
For each credit (`CR`) row, skipping GoCardless payouts, Stripe transfers and Eventbrite:
- classify: description contains `SUB`/`MEMBERSHIP` → subscription; contains `-BALANCE-<ref>` → credit top-up; else unknown;
- match a member by their `import_match_string`, else by **family name appearing anywhere in the description**;
- subscription with an open charge → mark that charge paid on the row date; without one → treat as a credit top-up;
- create a `standing-order` payment; for standing-order members, update `monthly_subscription` to the amount paid.

⚠️ Matching on family name is unreliable (common surnames, partial matches). **New:** show matches for confirmation
before saving, require a unique match, and remember confirmed matches.

## 4.7 Member credit

- **Balance = Σ(payments with reason `balance`, status paid/pending/withdrawn) − Σ(payments with source `balance`, same statuses)**.
- Legacy stored this as `cash_balance` and recomputed it after payments and nightly at 03:00; the new system calculates it on demand.
- Member credit page: balance, paginated history of credit movements, top-up options, and a **withdrawal request**
  form (amount, sort code, account number) which **emails the trustees**.

**New:**
- **Decided:** credit is calculated from the ledger whenever it's needed (a database query or view), not stored. That removes the nightly recalculation job and a whole class of drift bugs.
- **Don't email bank details.** Store the withdrawal request in the app for a trustee to action, visible only to finance/admin.

## 4.8 Finance views

- **Payments list** (`finance`): filter by date range, member, reason, source; sort; total; delete (cash payments only, and only
  those to/from credit); "assign unknown payment to member"; "refund to credit" (donations and inductions).
- **Payments overview**: credit liability (credit paid in − paid out), box fees and locker rent collected, unpaid locker rent, key deposits held,
  laser-cutter investment vs fees collected.
- **Subscription charges list** (paginated, all members).
- **Stats** (any member): member counts by payment method, distribution of subscription amounts, average subscription. (The legacy page on old-style
  Direct Debit migration progress is dropped with that method.)

## 4.9 Money summary and bank reconciliation (requested by a trustee, 2026-11-07)

A trustee asked for income to be summarised as a total and by heading, and noted that BBMS figures don't fully
agree with the bank statement.

### Why BBMS doesn't match the bank today (from the legacy code)
- **GoCardless fees are recorded as £0**, and BBMS ignores the `paid_out` event. GoCardless pays the bank in
  **batches, net of its fees**, so a bank credit can't be matched to BBMS payments.
- **Stripe fees are an estimate** (2.4% + 20p, hard-coded), not the real fee; Stripe also pays out in batches.
- Money taken **outside BBMS** isn't there: box fees before September 2024 (06 §6.2), standing orders unless the
  statement import was run, cash unless an admin recorded it.
- **Dates differ:** BBMS dates a Direct Debit when it's confirmed; it reaches the bank days later, so totals near a
  month end fall in different months.
- Refunds to credit and reclassified payments change a payment's reason after the event.

### Income summary (finance and admins)
For a chosen period (month by default), with a chart of each heading month by month so trends show:

| Heading | What counts |
|---|---|
| **Monthly fees** | Subscription payments |
| **One-off payments** | Induction fees, box fees, key deposits, donations |
| **Ongoing use** | Equipment usage fees, locker rent, consumables and other charges taken from credit |
| **Member credit added** | Credit top-ups and approved expenses credited (shown separately: this is money held for members, not income until spent) |

- Income is shown **gross**, with **provider fees** (GoCardless, Stripe) and **net** totals.
- Spending from credit counts as income when it's spent (under its heading), so top-ups and spending aren't counted
  twice. Credit owed to members is shown as a balance.
- Refunds and corrections appear as negative entries under the heading they correct.

### Reconciliation with the bank (finance)
- BBMS imports **GoCardless payouts** and their line items (payments and fees) through the GoCardless API, and
  **Stripe payouts** through Stripe's API, so each lump sum arriving at the bank is matched to the payments and fees
  inside it, with the **real fees**.
- A monthly **"money received" view by source** (GoCardless payouts, Stripe payouts, standing orders, cash), dated
  by when money reached the bank, to compare line by line with the bank statement.
- The bank statement import (4.6) marks each bank line as matched (to a payout, a standing-order payment or cash)
  or **unmatched**, so differences are listed rather than hidden.
- ⚠️ Historic figures before cutover will still have gaps (missing fees, money taken outside BBMS). Reconciliation
  is reliable from cutover onwards; GoCardless and Stripe payout history can be imported back as far as their APIs
  allow.

### Expenditure (nice to have)
BBMS only knows about **reimbursed member expenses** (06 §6.4), not the space's other spending (rent, utilities,
materials). The space has managed without expenditure tracking for years, so this is **later and optional**:
summarise approved expenses by category, and optionally let finance record or import other spending by heading,
to give income against expenditure per month.
