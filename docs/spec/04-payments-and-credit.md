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
| `locker-rent` | the locker's monthly rent (the default for its size and location, or a trustee's override for that rental) | Pays a locker rent charge (06 §6.2). Billed monthly with the subscription. |
| `balance` | any | Adds to member credit (top-up). |
| `equipment-fee` | computed | Taken from credit when a session ends (05 §5.4, §5.6). If an admin later adjusts the session's times, a correcting `equipment-fee` entry (a refund or an extra charge) is added; the original is never edited. |
| `withdrawal` | any | Credit paid out (cash) by an admin. |
| `donation` | any | PayPal money from someone BBMS can't match to a member (4.4). Finance can assign it to a member or refund it to their credit. |
| `costs`, consumable service names | any | Deducted from credit. |

**Decided:** the key deposit and box fee are admin-editable **settings** (02 §2.8) and locker rents come from the default rents per size and location (06 §6.2), not code. The amount charged is always worked out **on the server** from the reason and these settings, never taken from the browser.

### Sources

| Source | How it's created |
|---|---|
| `gocardless-variable` | App creates a GoCardless *payment* against the member's mandate (subscription billing, or member clicks "pay by DD"). Status starts `pending`, updated by webhook. |
| `stripe` | Card payment via Stripe Checkout/token; recorded `paid` immediately with fee = 2.4% + 20p (estimated, hard-coded). |
| `paypal` | PayPal subscription payment or credit top-up, recorded when PayPal's IPN says it's completed, with PayPal's fee (4.4). |
| `balance` | Spent from member credit. |
| `cash` | Admin records cash received. |
| `standing-order` | Bank statement import (finance). |
| `manual` | Admin-recorded (e.g. induction paid outside the system). |
| `reimbursement` | Approved expense credited to member. |

### Where a member can pay what (legacy "payment module")

| Payment for | Allowed methods |
|---|---|
| Subscription (monthly) | Direct Debit, credit, standing order, **PayPal subscription** (4.4) |
| Subscription (when there's a problem) | GoCardless, Stripe, credit |
| Induction fee | GoCardless, credit, Stripe |
| Key deposit | GoCardless, Stripe, credit |
| Box fee | GoCardless, credit, Stripe |
| Locker rent (monthly) | GoCardless (Direct Debit mandate), credit |
| Credit top-up | GoCardless, Stripe, **PayPal** (4.4) |
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

Built since (03 §3.6 build status): GoCardless (set-up, billing, webhook, retries), subscription and locker rent
charges and billing, and "subscription" in "Record a payment". The webhook stores every event, ignores duplicates,
handles them in the order GoCardless created them, and keeps failures for review (admin: GoCardless events).

Built since: card payments through Stripe (4.3), and the member's **Pay** page (`/pay/`), which lists what they can
pay (as in the table above) with "Pay by card", "Pay by Direct Debit" (members with a mandate) and "Pay from
credit", plus top-ups by card or Direct Debit on the credit page. A one-off Direct Debit (04 §4.2, "pay by DD"):
- is a GoCardless payment from the member's mandate, recorded `pending` until GoCardless confirms it;
- counts at once, as in legacy: a pending top-up is credit, a pending induction fee marks the induction paid, a
  pending key deposit or box fee counts as paid, and a failed charge paid this way becomes `processing`;
- is undone if it fails: the ledger row becomes failed (so a top-up's credit goes again, which can take credit
  below £0), the induction is unpaid again, and the member is emailed. One-off payments aren't retried; only
  subscription and rent charges follow the retry setting;
- asked for twice on the same day, for the same thing and amount, gives the same payment.

Still to build: the statement import (4.6), the finance
overview and money summary (4.8, 4.9), and importing the legacy payments (08 §8.2). The key deposit and box fee are admin settings (02 §2.8, built 2026-11-07), read through
`apps/payments/prices.py`.

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

### Build status (2026-11-08)

Built in `apps/payments` (`checkout.py` for the rules, `cards.py` for Stripe). Choices made:
- Members pay on **Stripe's hosted Checkout page** (cards only), so card details never reach BBMS. Each checkout is
  a `CardCheckout` row with the amount BBMS worked out; the browser only says *what* to pay.
- A payment is recorded when Stripe says it's paid, by the **webhook** (`POST /stripe/webhook`, signature checked
  with `STRIPE_WEBHOOK_SECRET`; events stored and repeats ignored) or when the member **comes back** to BBMS,
  whichever is first. Both are safe to happen twice.
- The **real fee** comes from the payment's balance transaction. If Stripe hasn't made it yet, the fee is recorded
  as £0 and a warning logged (the payout import in 4.9 will have the exact figure).
- If what was paid for has been paid another way meanwhile (say a Direct Debit came through, or an admin recorded
  cash), or Stripe took a different amount, the money goes to the member's **credit**, with a note; it's never lost.
- **Top-ups** (by card or Direct Debit) are limited by two settings: £5 to £200 to start with (the providers'
  fixed fees make small top-ups costly).
- What can be paid where follows the table in 4.1: charges that are due or failed, induction fees, the key deposit
  (trusted members), the box fee, and top-ups (not from credit; PayPal top-ups are in 4.4). Paying from credit is refused below £0.
- Without `STRIPE_SECRET_KEY`, development and tests use a fake (`/dev/stripe/…`), and a live site says card
  payments aren't available.
- Refunds are done in Stripe's dashboard and recorded by an admin for now; BBMS logs `charge.refunded` events but
  doesn't act on them.

## 4.4 PayPal (kept; decided 2026-11-08)

PayPal stays for two things: a **monthly subscription** (for members without a UK bank account: it costs the space
more than Direct Debit, so the pages say Direct Debit is preferred) and **credit top-ups**. Fees and charges aren't
paid by PayPal directly; a member tops up credit and pays from it. (Reverses the 2026-11-05 decision to drop it.)

### How legacy does it (`PaypalIPNController`)
- The member's account page has a PayPal **subscribe button** (`_xclick-subscriptions`, monthly, for their
  `monthly_subscription`). PayPal tells BBMS about payments by **IPN** at `POST /paypal-ipn`.
- `subscr_payment` with status `Completed`: the member is found by `payer_email` = their email or
  `secondary_email`. A `left` member is rejoined; a `setting-up` member gets payment method `paypal`, payment day
  today and a first charge. If a charge is open, the payment is a `subscription` payment for it, otherwise a credit
  top-up. Other statuses are only logged.
- No member found: recorded as a `donation` with no member, and the payer (cc the trustees) is emailed "Unknown
  PayPal payment received", asking them to add their PayPal address to their account.
- `subscr_cancel`: if the member still pays by PayPal, they start leaving.
- Leaving in BBMS doesn't cancel the PayPal subscription; the member is told to cancel it at PayPal.
- Grace period 7 days (03 §3.5). Fees come from PayPal (`mc_fee`), so they're real.
- There are no one-off PayPal top-ups: credit only comes from a subscription payment with no open charge.

### What the new system does
**A new PayPal account** (decided 2026-11-08): the space switches to a brand-new PayPal account at cutover. A
PayPal subscription belongs to the account it was made with, so the members paying by PayPal today **re-subscribe**
to the new account from BBMS, then cancel the old subscription (08).

**Integration.** PayPal's **subscribe and "buy now" buttons with IPN** at `/paypal-ipn`. No PayPal API credentials
are needed: BBMS only builds the buttons and checks IPNs. Each button sets its own `notify_url`, so nothing needs
setting in the PayPal account. ⚠️ Check when opening the new account that it can still use subscribe buttons and
IPN (PayPal calls them older products). If not, use PayPal's REST Subscriptions and Orders APIs with webhooks
instead; the rules below stay the same.

**Every IPN**:
- is **verified with PayPal** first (sent back with `cmd=_notify-validate`); anything not `VERIFIED` is refused
  and logged, and nothing is recorded;
- must be to the space's PayPal account (`PAYPAL_ACCOUNT_EMAIL`, or during the switch one of
  `PAYPAL_OLD_ACCOUNT_EMAILS`; see below) and in GBP, or it's kept for a person to look at;
- is stored, and a repeat (same `txn_id` and `payment_status`, or the same `ipn_track_id`) is ignored; stored IPNs
  are handled in order, and one that fails is kept with its error for review, like GoCardless events (4.2). A
  payment is recorded once per `txn_id`.

**Finding the member**, in this order: the PayPal subscription id stored on the member (`subscr_id`); the `custom`
field (a signed member reference that BBMS puts on every button it builds); the payer's email matching the member's
email or their **PayPal email** (legacy `secondary_email`, kept and editable on the account page). The first match
stores the `subscr_id` on the member, so later payments don't depend on the email.

**Subscription** (`txn_type`):
| PayPal says | Effect |
|---|---|
| `subscr_signup` | Store `subscr_id`. The member gets payment method `paypal` and, if they have none, payment day today (the 1st after the 28th). A `setting-up` or `leaving` member is *activated* (03 §3.4); the first charge is due today. A member who has left isn't set up: they rejoin first (03 §3.8), which makes them `setting-up`. |
| `subscr_payment`, `Completed` | Recorded as source `paypal` with PayPal's fee. It pays the member's oldest `due` or `failed` subscription charge (amount: what PayPal took); if none is waiting, it goes to **credit** (PayPal sometimes pays a day or two before the charge date). If no `subscr_signup` was seen, the first payment sets the member up as above. |
| `subscr_payment`, `Pending` | Logged; PayPal sends `Completed` later (same `txn_id`). |
| `subscr_failed` | Logged; the member is emailed that PayPal couldn't take the payment and will try again. PayPal retries by itself; BBMS doesn't. |
| `subscr_cancel`, `subscr_eot` | If the member still pays by PayPal, they start **leaving** (03 §3.8): access until their paid-until date. Otherwise logged only. |

**Billing a PayPal payer** (03 §3.6): charges are created as for everyone. At the nightly run, a due charge is taken
from credit if there's enough (that's where an early PayPal payment went); otherwise it waits for PayPal, with no
"credit too low" email. The usual grace period (7 days, a setting) and suspension rules apply after that.

**Amount.** A PayPal subscription is for a fixed amount, set when the member subscribes. Changing the monthly amount
(03 §3.7) means a new PayPal subscription: BBMS shows a new subscribe button and tells the member to cancel the old
one at PayPal. A charge is paid with what PayPal actually took.

**Top-ups.** The credit page and the Pay page get **"Top up by PayPal"**: a one-off PayPal payment (`_xclick`, GBP)
for an amount the member chooses within the top-up limits (4.3), with `custom` set. The `web_accept` IPN with status
`Completed` records a `balance` payment from `paypal` for **what PayPal says it took** (`mc_gross`), so editing the
amount in the browser only changes how much credit is bought. An amount outside the limits is still recorded (the
money was taken) and logged. The credit shows once PayPal confirms it, not before.

**Refunds and reversals.** A `Refunded` or `Reversed` IPN (with `parent_txn_id`) adds a correcting negative entry
linked to the original. If the original paid a subscription charge, that charge becomes `failed` (03 §3.6); if it
was a top-up, the credit goes down (it may go below £0; finance is told). `Canceled_Reversal` undoes the reversal.

**Leaving.** BBMS doesn't cancel a PayPal subscription; the "Leave" page tells PayPal payers to cancel it at PayPal,
as legacy does. A payment from a member who has **left** is put in their credit and finance is told; it doesn't
rejoin them (legacy did).

**Unknown payers.** No member found: the payment is recorded with no member, reason `donation`, and listed for
finance to assign to a member (4.8). The payer is emailed (as legacy, without a copy to the trustees' inbox) and
finance is notified in BBMS.

**Configuration (environment variables, not admin settings, since changing the account is a deployment job):**
- `PAYPAL_ACCOUNT_EMAIL`: the space's PayPal account (the new one). Every button BBMS shows pays this account, and
  IPNs must be for it. Unset: PayPal buttons aren't shown and IPNs are refused (development and tests use the fake).
- `PAYPAL_OLD_ACCOUNT_EMAILS` (optional, comma-separated): accounts whose payments BBMS still **records** but never
  asks members to pay. For the switch-over: point the old account's IPN URL at the new BBMS, and PayPal payers'
  old subscriptions keep counting until they've moved. Remove it once they have.
- `PAYPAL_SANDBOX`: use PayPal's sandbox (buttons and IPN checks) instead of the live site.
When developing and testing, a fake PayPal page sends BBMS a made-up IPN (like the fakes for Stripe and GoCardless).

### Build status (2026-11-08)

Built in `apps/payments` (`paypal.py` for the buttons and checking with PayPal, `paypal_ipn.py` for the rules above).
Choices made while building:
- **Who sees PayPal:** "Subscribe with PayPal" is on `/account/payment/` for members setting up (or leaving), below
  Direct Debit and marked as costing the space more. A PayPal payer sees "Start a new PayPal subscription for
  £X a month" there instead (for a changed amount) and can switch to credit, or to Direct Debit by setting one up;
  either way they're told to cancel the PayPal subscription. Members paying another way aren't offered PayPal.
  "Top up by PayPal" is next to the other top-up buttons on the credit and Pay pages.
- **The PayPal email** is on the member's "Edit your details" page (optional; refused if another account uses that
  address). The import fills it from legacy's `secondary_email` and keeps payment method `paypal`.
- **Notifications** are stored as `PayPalNotification` rows (admin: Payments → PayPal notifications, with "try
  again" for failed ones, e.g. after adding an old account to `PAYPAL_OLD_ACCOUNT_EMAILS`). One for another account,
  in another currency, or refunding a payment BBMS doesn't have is kept with that error and nothing is recorded.
- A subscription's id is stored when it signs up (replacing an older one), or with the first payment matched if the
  member has none (imported members). So cancelling an **old** subscription after re-subscribing changes nothing.
- **Unmatched money** is recorded as a `donation` with no member. Finance (the `finance` role) is emailed with a
  link to the payment, where they can choose the member: it then becomes a top-up of that member's credit. The
  payer is emailed too. Payments from members who have left, and refunds or reversals, are emailed to finance.
- A refund's or reversal's fee isn't recorded (PayPal returns part of it); the correction is for the full amount.
- Fees come from PayPal (`mc_fee`).
- Without `PAYPAL_ACCOUNT_EMAIL`, development and tests use a fake: `/dev/paypal/` stands in for PayPal's page, and
  `/dev/paypal/tools/` sends the next monthly payment, a failed payment, a cancellation or a refund.

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
