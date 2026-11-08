# 3. Membership lifecycle

## 3.1 Joining

**Registration form** (`/register`, public):

| Field | Rule |
|---|---|
| Given name, family name | required |
| Email | required, valid, unique |
| Password | required, min 8 |
| Phone | required, min 10 chars |
| Address line 1, postcode | required; postcode must be a valid UK postcode |
| Address lines 2–4 | optional |
| Monthly subscription | required, integer pounds, at least the **minimum monthly subscription** setting (02 §2.8; £25, was £20 in legacy) |
| Emergency contact | required |
| Profile photo | optional upload; goes into the **pending approval** queue |
| Photo private | optional |
| Agree to rules | must be ticked (stores `rules_agreed` timestamp) |
| "I have visited the space" | must be ticked |

Admins can register someone on their behalf; phone and emergency contact become optional and the address is auto-approved.

On submit: user created with `status = setting-up`, `active = false`; an empty profile is created; the member is logged in
(unless an admin created the account) and sent to their account page.

**Next steps shown to a setting-up member:**
1. **Set up payment** — either
   - **Direct Debit** (GoCardless redirect flow → mandate). On return: store `mandate_id`, `payment_method = gocardless-variable`, `payment_day = today's day`, then *activate* (§3.4); or
   - **Pay from credit** (`payment_method = balance`) — only offered if the member has no payment method and is `setting-up`/`leaving`/`left`. Sets `payment_day = today`, then *activate*.
2. **Proof of identity** (`/account/id-documents/`): upload photo ID and proof of address (below).
3. **General induction** (`/account/{id}/induction`): read the member agreement (markdown policy), tick "rules agreed" and "induction completed". Then a `comms` role holder confirms it from `/member_inductions` (sets `inducted_by`).
4. On their first visit, get a free fob (to tap in and use machines). Optional: book equipment inductions, pay for a box, rent a locker.

**Welcome email** is sent the moment status first changes away from `setting-up`.

### Proof of identity (added 2026-11-07; built)

New members prove who they are with **two documents**: **photo ID** (passport, driving licence) and **proof of
address** (a utility bill or bank statement from the last three months). Until now they emailed these to the
trustees, which leaves copies in mailboxes indefinitely. **BBMS replaces that**: members upload them, a trustee checks
them, and BBMS deletes them. Members are told not to email them.

- **Uploading** (member, `/account/id-documents/`): a scan or photo of each, sent together or one at a time. A new
  upload replaces the waiting one of the same kind, and a member can delete an upload before it's checked.
  - Accepted: PDF, JPEG, PNG, WebP and HEIC (iPhone photos), **recognised from the file's contents**, not its name or
    the browser's claim. Anything else (including HTML, SVG and Office files) is refused.
  - At most **10 MB** each. Uploads are held in memory while the request is handled, never written to a temporary
    file, and an over-size request is refused before it is read.
- **Checking** (`/id-checks/`, anyone with the permission **"Can view and check new members' proofs of identity"**:
  admins have it; give it to the trustees' role): a list of members waiting, oldest first. The trustee opens each
  document in a new tab (HEIC downloads instead, since most browsers can't show it), compares it with the name and
  address given, and records **approved** or **not accepted** with a note (required when not accepted, e.g. "the
  bill is from 2019"). The member's account page then shows the result; when not accepted, they upload again.
- **Shredding:** recording the decision **deletes the documents, whatever the decision**, in the same database
  transaction. What is kept permanently is an **ID check** record: who checked, when, the result, the note and which
  kinds of document were seen. Uploads nobody checks are deleted after **30 days** (a setting; daily job
  `shred_stale_id_documents`). Deleting a member deletes their uploads.
- **Where they're stored:** in the database (one table, `IdDocument`), not in file storage. There is then exactly one
  copy, deleting the row deletes the file, and nothing depends on App Platform's temporary disk or a Spaces bucket.
  The table is **never** shown in the Django admin, **never** migrated and **never** copied to the staging or shadow
  sites (08). Every view of a document and every check is logged (member id and reviewer id, not the content).
  Documents are sent with `Cache-Control: no-store`, so browsers don't keep a copy.
- **Encrypted at rest** (built 2026-11-07). Each document is encrypted before it reaches the database (Fernet from
  the `cryptography` package: AES with an HMAC, so a tampered document won't open), with a key held only in the
  app's environment, `ID_DOCUMENT_KEYS`. Database backups and dumps hold only ciphertext, and the staging and shadow
  sites never get the key. The file name, type and size aren't encrypted. Without a key, production **refuses
  uploads** rather than store a plain copy (`manage.py check --deploy` warns), and a startup check rejects a key
  that isn't valid. When developing, a key is worked out from `SECRET_KEY`.
  - **Changing the key:** put the new key first in `ID_DOCUMENT_KEYS` with the old one after it, deploy, run
    `manage.py rotate_id_document_key`, then remove the old key. A document no key opens shows the trustee a
    message asking them to record it as not accepted and ask the member to send it again.
  - Uploads from before encryption are encrypted by a migration (it stops if there are any and no key is set).
  - "Deleted" still means gone from the live database at once, and gone from backups within their retention period
    (DigitalOcean's backups: 7 days), but a backup copy can't be read without the key. Say so in the privacy
    notice.
- ❓ **For the trustees:** whether an unchecked or not-accepted ID **blocks** anything (today it doesn't: it's a
  step on the account page and the trustees decide what follows); who checks (suggested: trustees); how long
  unchecked uploads are kept (suggested 30 days); whether existing members who emailed documents need re-checking
  (suggested no, but delete the emails).

## 3.2 Statuses

| Status | Access to space | Billed | Meaning |
|---|---|---|---|
| `setting-up` | No | No | Registered, no payment set up yet. |
| `active` | Yes | Yes | Paid up (or within grace period). |
| `payment-warning` | Yes | Yes | **Legacy** — handled by the nightly job but nothing in the current code sets it. ⚠️ Verify; probably drop. |
| `suspended` | **No** (`active = false`) | Charges still outstanding | Payment missed beyond the grace period. |
| `leaving` | Yes, until `subscription_expires` | No new DD | Member (or GoCardless) cancelled; access continues to end of paid period. |
| `on-hold` | — | — | Display only; no logic. ⚠️ Verify; probably drop. |
| `left` | No | No | Former member. Can rejoin (→ `setting-up`). |
| `honorary` | Yes | **Never** | Excluded from billing and all nightly checks. |

Separately, a member can be **banned** (`banned = true`): they can log in but every protected page refuses them.

```
setting-up ──(payment set up)──▶ active ──(cancel DD / mandate cancelled)──▶ leaving ──(expiry passes)──▶ left
                                   │  ▲                                                                  │
                    (expiry < grace cutoff, no newer paid charge)                                       (rejoin)
                                   ▼  │(paid charge found)                                               ▼
                               suspended ──(next run, still unpaid)──▶ left                         setting-up
```

**Emails on transition:** → `payment-warning` (warning email), → `suspended` (suspended email), → `left` (left email, mentions any lockers held, and their box if they paid for one).

## 3.3 Subscription charges

A **subscription charge** is "this member owes this month's subscription, due on this date". It is the link between
billing and the membership expiry date.

Lifecycle: `pending` (created in advance) → `due` (on/after the charge date) → `processing` (payment submitted) → `paid`;
or `cancelled` (member left). In legacy a failed payment sent it back to `due` (so it was billed again every night); in the new system it stays
`processing` while GoCardless retries and becomes `failed` if the retries fail (3.6).

Paying a charge extends `subscription_expires` to **payment date + 1 month** and sets the member `active`.
A charge going to `processing` already extends the expiry to **charge date + 1 month** (Direct Debit takes days to clear).

## 3.4 Activation ("ensure membership active")

Called after payment set-up. Sets `active = true`, `status = active`. Then, if the member has **no outstanding charges**
(pending/due/processing) **and** their expiry is empty or in the past, it creates an immediate charge dated today
(or the 1st of next month if today is after the 28th and payment day is 1):
- for `gocardless-variable`: creates the charge as `due` and immediately bills the mandate;
- otherwise: creates the charge as `due` (billed from credit at the next nightly run).

## 3.5 Grace periods

A member is only suspended when `subscription_expires` is earlier than *today minus the grace period* for their payment method:

| Method | Grace |
|---|---|
| `gocardless-variable` | 10 days |
| `standing-order` | **6 months** |
| anything else (`balance`, `cash`, …) | 7 days |

(Legacy also had 14 days for old-style `gocardless` and 7 days for `paypal`; both methods are dropped.)

These are **settings** in the new system (02 §2.8), starting with the legacy values, which are **kept** (decided
2026-11-07, provisionally; easy to change later).

## 3.6 Nightly jobs (scheduler)

All times Europe/London. Each must be **idempotent** and must **alert on failure** (legacy used Envoyer heartbeats).

| Time | Job | What it does |
|---|---|---|
| 01:00 | `create-todays-sub-charges` | Also creates a pending **locker rent** charge for each rented locker on the same dates (06 §6.2). For each date from **today+7 back to today**, for every billable member (`active`, not `honorary`) whose `payment_day` equals that date's day, create a `pending` charge if none exists for that date. |
| 01:30 | `bill-members` | Bills subscription and **locker rent** charges together: Direct Debit payers get **one** GoCardless payment per payment day covering both; credit payers have both taken from credit. (1) Every `pending` charge dated today or earlier → `due`. (2) For each `due` charge: **balance** payers — if credit ≥ subscription, record a `subscription` payment from `balance` (marks the charge paid); otherwise, if their backup method is Direct Debit, bill that and email "trying DD", else email "payment failed". **gocardless-variable** payers — create a GoCardless payment for `monthly_subscription`, record it as `pending` against the charge (charge → `processing`). |
| 02:00 | `calculate-equipment-fees` | Backstop: bill any finished, unbilled equipment sessions (sessions are normally charged when they end; 05 §5.4, §5.6). |
| 06:00 | `check-memberships` | Four passes, in this order (below). |
| 07:00 (new) | `retry_direct_debits` | In "BBMS retries" mode, asks GoCardless to retry failed Direct Debits whose retry day has come. **Built** |
| every 15 min (new) | `process_gocardless_events` | Backstop for webhook events that were stored but not handled. **Built** |
| daily (new) | `shred_stale_id_documents` | Delete proofs of identity nobody has checked within the time limit (§3.1, "Proof of identity"). |
| hourly | `fix-equipment-log` | Close stale sessions; merge sessions with gaps ≤ 60 s; mark sessions ≤ 60 s as removed. |
| hourly | `calculate-proposal-votes` | Tally proposals whose end date has passed. |
| every 15 min (legacy: 10) | `device:check-online` | Notify `acs` role holders when a monitored device's last heartbeat is > 1 hour old (deduplicated per heartbeat). |

The legacy 03:00 `update-balances` job is not needed: credit is calculated from the ledger on demand (04 §4.7).

**`check-memberships` passes:**
1. **payment-warning** members past expiry: if they have no paid charge, or their paid-until date is more than 2 days before the grace cut-off → **left**.
2. **suspended** members: if a paid charge now covers a later date → **re-activate** to that date; otherwise, once they've
   been suspended for **30 days** (a setting; decided 2026-11-07, provisionally) → **left**. Legacy marked them left the
   next morning.
3. **leaving** members (not honorary) past expiry → **left**.
4. **active** members (not honorary) whose expiry is missing or before the grace cut-off: if a paid charge covers a later date → extend; otherwise → **suspended**.

"Paid-until" = the member's most recent **paid** charge's `charge_date` + 1 month.

In the legacy system pass 2 ran the morning after pass 4, so an unpaid member was **suspended for one day and then
marked left**. The new system keeps them suspended (no access, Direct Debit kept) for 30 days first, so a late
payment simply re-activates them.

**Failed Direct Debits** (decided 2026-11-07, provisionally): **never re-billed nightly** (legacy put the charge back
to `due`, so it was billed again at every 01:30 run). How retries happen is an **admin setting**:
- **"Let GoCardless retry"**: for accounts with GoCardless's automatic retries (Success+). BBMS waits: while GoCardless
  says it will retry, the charge stays `processing`.
- **"BBMS retries"** (the **default**, since the space probably doesn't have Success+): BBMS asks GoCardless to retry
  the **same** payment (GoCardless's "retry a payment" action) after a set number of days, up to a set number of
  times (settings; suggested **once, after 5 days**). It only retries failures worth retrying (for example not enough
  money in the account), never a cancelled mandate.
- In both modes the member is emailed after the first failure. When a payment has **finally** failed, the charge
  becomes `failed`, the member is emailed, and the grace period and suspension rules apply. The member can pay it from
  their account page (credit, card or a one-off Direct Debit), or an admin can retry it.
- ⚠️ Verify the GoCardless account allows the "retry a payment" action, and any limits on how often a payment may be
  retried.

**"Becomes left" side effects:** locker rentals end (the storage team is told to clear and release their lockers, and to look for their labelled box if they paid for one), `active = false`, `status = left`, `payment_day` cleared, all pending/due charges cancelled, left email sent.

### Subscriptions and billing: build status (2026-11-07)

Built in `apps/billing` (charges, GoCardless, the jobs) with the member's pages at `/account/payment/`. Choices made
while building (change them in the code if the trustees decide otherwise):

- **One charge table for subscriptions and locker rent** (`Charge`, with a `kind`), so a Direct Debit payer's
  subscription and rent go in **one GoCardless payment**, with one ledger row per charge sharing its payment id.
- **The amount is taken when a charge is billed**, not when it's created a week ahead, so a new monthly amount
  applies to the next charge not yet billed.
- **Activation** (§3.4) as specified: a credit payer's first charge is due straight away but taken from credit at
  the next nightly run; a Direct Debit payer's is billed at once.
- **Credit payers** without enough credit are emailed once per charge, and it's tried again each night. With a
  mandate, Direct Debit is their backup (a credit payer with a mandate *is* the "secondary method").
- **Failed Direct Debits:** in "BBMS retries" mode, only `insufficient_funds` and `refer_to_payer` are retried
  (once, after 5 days, by default; other causes are final). While a retry is due, the charge stays `processing` and
  the paid-until date stays where it was; when it finally fails, the paid-until date goes back to the last paid
  charge + 1 month, or the failure date. The member is emailed each time it fails. A charge-back marks the charge
  failed too.
- **Mandate cancelled, failed or expired** (webhook): a Direct Debit payer starts leaving; a credit payer just loses
  the backup. `mandates.replaced` (the member's bank changed) moves them to the new mandate.
- **Suspended members with no recorded suspension date** (imported ones) get today's date, so the 30 days start
  from the first check after cutover rather than marking them left at once.
- **Leaving** (the member's "Leave", or a cancelled mandate) cancels the mandate at GoCardless and charges not yet
  billed. Locker rent isn't charged while leaving; lockers are released when they become `left`.
- **Becoming left** ends locker rentals, cancels charges not yet billed, clears the payment day, and emails the member
  and (if they had lockers or a paid box) the `storage` role.
- **Honorary** members are never charged a subscription or suspended, but pay locker rent.
- Direct Debit set-up uses GoCardless's **billing request flow** (its current hosted pages; the older "redirect
  flow" is the same idea). If the member doesn't come back to BBMS, the `billing_requests.fulfilled` webhook
  finishes the set-up. A new mandate replaces (and cancels) an old one.
- **Emails** (plain text, `templates/billing/emails/`): welcome, credit too low, trying Direct Debit, Direct Debit
  failed, suspended, leaving, left, and to the storage team. Dry runs send none.
- Admins: charges (read-only, with "retry the failed Direct Debit" and "cancel"), GoCardless events (with "try failed
  events again"), Direct Debit set-ups, and billing fields on the member's page. A subscription paid in cash is
  recorded with "Record a payment" and pays the oldest unpaid charge (or one dated today).

Still to build: card payments (Stripe), the standing-order statement import (04 §4.6), self-service locker renting
with its first pro-rata payment (06 §6.2), shadow-site comparisons (08), and a heartbeat alert per job.

## 3.7 Changing a subscription

- **Amount** (member): only if method is `gocardless-variable` or `balance`. Minimum is a setting (02 §2.8: **£25**, decided 2026-11-07; legacy £20; admins may go down to £5). Applies from the next charge. Raising the minimum doesn't change what existing members pay: a member below it keeps their amount until they change it, and must then choose at least the new minimum.
- **Method** (member): `gocardless-variable` ⇄ `balance`. Switching to balance keeps Direct Debit as the **backup** method.
- **Method/day** (admin): any method and payment day 1–28.
- **Migrate old DD**: not rebuilt. Any member still on an old-style `gocardless` subscription is moved to a variable mandate before cutover (08).

## 3.8 Leaving and rejoining

- **Member cancels Direct Debit**: mandate cancelled at GoCardless; `mandate_id` and method cleared; status → `leaving`;
  pending/due charges cancelled. Access continues until `subscription_expires`; then the 06:00 job marks them `left`.
- **Mandate cancelled at GoCardless** (bank, or member via their bank) → webhook → same as above.
- **"Leave" button**: a `setting-up` account is **deleted outright**; anyone else → `leaving`.
- **Rejoin** (a `left` member): status → `setting-up`, then the normal payment set-up flow.

### The member's account page: build status (2026-11-07)

Built (`/account/`): membership status, monthly amount, credit (linked to the credit page, 04 §4.7), fobs, machines
trained on, what the front door does for them (from the door rules, 05 §5.2), proof-of-identity status and, for a
`setting-up` member, the next steps. A member can:
- **edit their details**: name, phone (at least 10 characters) and emergency contact, all required. The **email
  address** is the login, so changing it is an admin job for now (it needs confirming by email first);
- **change their address**: the new one waits for a trustee and the old one stays current until then (02 §2.3). A
  second change replaces a waiting one. Trustees approve or decline in the admin ("Addresses", filtered to "not
  approved"); approving deletes the out-of-date addresses;
- **change the monthly amount** (§3.7, at least the minimum setting, £25). Until billing exists this only stores
  the amount;
- **change their password** (Django's page, in the site's style);
- **leave, if still `setting-up`**: the account is deleted with its address, uploads and ID checks. If anything that
  must be kept refers to it (a payment, a fob), it isn't deleted and the member is asked to contact the trustees.

Since the subscriptions work: payment method and day, "paid until", the charges list (on `/account/payment/`), the
"method" change, leaving for current members (cancel the Direct Debit → `leaving`) and rejoining. Members paying by
standing order or cash ask a trustee to change their amount. Waiting for other apps: the profile and photo
(06 §6.1), the general induction step (05 §5.9) and emails about address decisions.

## 3.9 Admin member controls

From the member's account page, admins can:
- toggle **trusted** (sends "you are now trusted" email + in-app notification), **key holder**, **induction completed**, **photo on wall**;
- **approve/decline** a pending photo (declining emails the member) and a pending address change;
- change payment method & day; record cash payments and cash withdrawals from credit; record manual payments;
- edit any field (bypassing phone/emergency-contact requirements);
- see a "trusted members missing photos" report (`/account/trusted_missing_photos`).

All changes to `trusted`, `key_holder`, `induction_completed` are written to the **audit log**.
