# 8. Migration and cutover

## 8.1 Principles

- The door and the machines must keep working throughout. **No big-bang switch without a rollback path.**
- Billing must never run in **both** systems on the same night (double debits).
- Migrate data with a **repeatable script** (run it many times against copies of production before the real cutover).

## 8.2 Data migration

| Legacy | New | Transform |
|---|---|---|
| `users` + `profile_data` + approved `user_address` | Member, Profile, Address | Money → pence; normalise statuses (`payment-warning`, `on-hold` ⇒ map ❓); drop unused columns, `secondary_email`, `subscription_id` and `cash_balance` (02). |
| pending `user_address`, `new_profile_photo` | pending approvals | Keep pending items so admins can still action them. |
| `roles`, `role_user` | Roles/groups | As is. |
| `key_fobs` | Fobs | **Normalise tag ids** (strip/add `BB` prefix consistently) so exact matching works; check for collisions. |
| `payments` | Ledger | `amount`, `fee` → pence (round half-up, then **reconcile totals per member and per reason** against legacy). |
| `subscription_charge` | Charges | As is. |
| `cash_balance` | (not stored) | Credit is calculated from the ledger. Check that the calculated balance equals legacy `cash_balance` for **every** member before cutover, and investigate any difference. |
| `inductions`, `equipment`, `equipment_log`, `access_log` | Same (legacy main-door 402 "Not a keyholder" entries become **check-ins**, giving historical attendance; 05 §5.2) | Equipment photos and money fields converted. Consider archiving old logs (retention ❓). |
| `storage_boxes` | (not migrated) | Boxes are no longer tracked individually. |
| "Member Box" equipment item and its inductions | Members' **box paid** flag | Decided 2026-11-07. A member gets the flag if they have an induction with key `Box` (the "Member Box" item: equipment id 41, slug `19l-box`, confirmed 2026-11-07) that is marked paid **and** whose linked payment has status `paid`. The legacy system creates a paid induction as soon as the payment is created, even when a Direct Debit is still pending, so a failed Direct Debit can leave a "paid" induction behind. The flag's date and payment come from that payment. Members whose induction looks paid but whose payment isn't are listed for the storage team to check, and the storage team ticks members who paid outside BBMS (all box payments before 4 September 2024, the first one recorded). Production check on 2026-11-07: 15 members flagged, none with a failed payment. The equipment item and those inductions are **not** migrated; the payments stay in the ledger. The box fee setting starts from the item's access fee. |
| Legacy £5 box deposits | Payment history | Obsolete (hidden from members); kept as history, not converted to credit. |
| Lockers | (new) | Created from the inventory the space supplies; no existing rentals. |
| `proposals`, `proposal_votes`, `expenses`, `notifications`, `audit_log` | Same | |
| `acs_nodes` | Devices | **Keep API keys** so current (v1) devices don't need reflashing. Fill in each fob's card number for v2. |
| `settings` | Settings | Not migrated (the emergency PIN is defunct). Create the new settings (02 §2.8) with the legacy values. |
| Proofs of identity (emailed to the trustees until now) | (not migrated) | New in BBMS (03 §3.1). Nothing to migrate: the emailed copies are outside BBMS. Trustees delete them from their mailboxes once uploads are live. |
| S3 photos, receipts | DigitalOcean Spaces (private) | Copy; regenerate keys; update references. |
| Passwords | Auth | Legacy stores bcrypt (`$2y$…`). Laravel reads these natively; Django needs the hashes prefixed into its `bcrypt$` format (or a small custom hasher) — then members don't need to reset. Prove this with a test during phase 1. |
| GoCardless mandates | — | Mandates live at GoCardless; just carry `mandate_id`. **Reuse the same GoCardless account** — no re-authorisation needed. |

**Before cutover:** move any member still on old-style GoCardless subscriptions to variable Direct Debit (that
method is dropped). PayPal is kept (04 §4.4, decided 2026-11-08), but with a new account, so PayPal payers subscribe
again (below). Records for
dropped features (CCTV, detected devices) aren't migrated.

**Members to move (counted on production, 2026-11-08; counts only).** After cutover the new system doesn't record
old-style GoCardless payments, so these members would be charged from credit or suspended while their old payments
carry on. 5 active members need moving to Direct Debit, and 5 PayPal payers need to subscribe again to the new
PayPal account:

| Group | Members | Paying now | Before cutover |
|---|---|---|---|
| Old-style GoCardless subscription, payment method `gocardless` | 3 | Monthly, through the old subscription | Set up a variable Direct Debit, **then** cancel the old subscription (cancelling first leaves a month unpaid). |
| Old-style GoCardless subscription, payment method `balance` | 2 | Monthly, through the old subscription (reason `subscription`); nothing taken from credit, so not paying twice today | The same, then set their payment method to Direct Debit. Otherwise the new system takes the subscription from credit as well as GoCardless. |
| PayPal | 5 | Monthly (3–5 PayPal payments a month over the last year) | The space moves to a **new PayPal account** at cutover (04 §4.4), so after cutover each one subscribes again from their BBMS payment page (to the new account), **then** cancels the old subscription at PayPal. Until they have, their old-account payments are recorded if the old account is in `PAYPAL_OLD_ACCOUNT_EMAILS` and its IPN URL points at the new BBMS; check each one's PayPal email is on their account so those payments match. |

50 members who have left are still marked `paypal`; they need nothing (a payment from one would go to their credit
and the trustees would be told). A trustee finds who is in each group from the live admin (names stay out of the
repo), contacts them, and re-runs the counts before cutover: every old-style GoCardless row should be 0.

The import also lists any left over, by legacy id. The queries are below.

**PayPal at cutover** (a new account, 04 §4.4):
1. Before cutover: open the new PayPal account and check it can use subscribe buttons and IPN. Set
   `PAYPAL_ACCOUNT_EMAIL` to it, and `PAYPAL_OLD_ACCOUNT_EMAILS` to the old account's email.
2. At cutover: in the **old** account's settings, point its IPN URL at the new BBMS (`…/paypal-ipn`). Legacy's
   buttons don't set their own, so the old subscriptions' IPNs go there. PayPal resends an IPN it couldn't deliver
   for a few days, so a short outage loses nothing.
3. Ask the PayPal payers to subscribe again from BBMS and then cancel the old subscription. Watch the old account
   until no subscriptions are left on it.
4. Then remove `PAYPAL_OLD_ACCOUNT_EMAILS`, move the remaining money and close the old account.

The queries:

```sql
-- Who pays by old-style GoCardless or PayPal, and how many still pay that way (last 3 months)
SELECT p.source, u.payment_method AS current_method, u.status, COUNT(DISTINCT p.user_id) AS members
FROM payments p JOIN users u ON u.id = p.user_id
WHERE p.source IN ('gocardless', 'paypal') AND p.status = 'paid'
  AND p.created_at >= NOW() - INTERVAL 3 MONTH
GROUP BY p.source, u.payment_method, u.status;

SELECT payment_method, status, COUNT(*) AS members
FROM users WHERE payment_method IN ('gocardless', 'paypal') AND status <> 'left'
GROUP BY payment_method, status;
```

### Import script status (2026-11-08)

`manage.py import_legacy` (code in `apps/legacy/`) reads the legacy MySQL with a read-only user and imports, into an
**empty** database: members (legacy passwords keep working, with how they pay, their payment day, mandate,
monthly amount and paid-until date, and who confirmed their general induction), their addresses, the audit log,
subscription charges, the **payments ledger**, roles, key fobs, devices (keeping their API keys), equipment,
inductions, machine sessions, the door log and (when asked) **photos**. It prints a report with counts, skipped rows
(by legacy id) and things to check; it never shows names or emails. `--dry-run` imports, reports and rolls back.

Options:
- `--mark-inducted`: every imported member except those still setting up gets a **confirmed general induction**
  (confirmed "by the legacy import", at the time of the import, and logged in their audit log). The door now needs
  one (05 §5.2) and legacy didn't check it, so without this, key holders whose induction legacy never confirmed lose
  24-hour access; the report lists how many. ❓ Trustees to decide whether to use it at cutover.
- `--photos-from URL`: copy members' photos (approved, and waiting for approval) from legacy's public S3 folder, e.g.
  `https://s3-eu-west-1.amazonaws.com/<bucket>/production`. They're stored in private storage (Spaces, 01 §1.8)
  under random names (`MemberPhoto`). Legacy names them by the md5 of `users.hash`. Needs internet access, so not on
  the clone droplet; in a dry run they're only checked. Photos that can't be fetched are listed; the member keeps
  their "photo approved" flag either way, so the door isn't affected.

Choices made in the script (change them there if the trustees decide otherwise):
- `payment-warning` → active (it still had access); `on-hold` → suspended. ❓
- Fobs that clash on tag or card number: the active, most recently changed one is kept; the rest are listed.
- The Member Box item and its inductions aren't imported; the members who qualify for the box-paid flag are listed
  (stored once the storage app exists).
- Every finished legacy session is marked as **settled**, so the new system never charges for it. Sessions the
  legacy system hadn't billed are counted in the report, with what they'd cost.
- Legacy `402` door entries become check-ins.
- Equipment charged per gram or per page has its usage cost set to 0 (per hour only), and is listed.
- Members paying by old-style GoCardless are imported with **no payment method** and listed by legacy id: move
  them to Direct Debit before cutover. PayPal payers keep payment method `paypal`, and legacy's `secondary_email`
  becomes their PayPal email (04 §4.4). A payment day after the 28th becomes the 1st.
- Subscription charges are imported as they are; a second charge for the same member and date is skipped and
  listed. Charges still `processing` at cutover finish when GoCardless's events arrive, matched through the imported
  payments' GoCardless ids.
- **Payments:** pounds become pence, rounding half up. `door-key` becomes `key-deposit`. Any other unknown reason
  (e.g. `printer`) becomes a consumable, with the legacy reason kept in the reference. Unknown sources become
  "recorded by an admin". GoCardless's statuses are mapped (`confirmed`, `paid_out` → paid; `pending_submission`,
  `submitted` → pending; `charged_back`, `customer_approval_denied` → failed; `refunded` → cancelled), and unknown
  ones become cancelled, which, as in legacy, doesn't count towards credit. Each kind of fallback is counted in the
  report. Payments of members who aren't imported, and PayPal donations from non-members, are kept with no member.
  Subscription payments are linked to their charge (the reference is the charge id), equipment fees to their
  session (`<session id>:<device>`) and induction fees to their induction (`inductions.payment_id`).
- **Credit is reconciled:** each member's credit, worked out from the imported ledger, is compared with legacy's
  cached `users.cash_balance`; differences are listed by legacy id with the amount.
- **Addresses:** each member's latest approved address, and a later change still waiting for approval (shown in the
  admin's Addresses queue). Older addresses, and changes older than the approved one, are skipped and listed.
  Postcodes are upper-cased.
- **Audit log:** each legacy row becomes a change with the field "legacy" and legacy's description.

Not imported yet (waiting for their apps): proposals, expenses, notifications, profiles' social links and
descriptions, equipment photos and receipts. Tested on the legacy clone's data inside the Docker image
(`tools/clone_test/`).

## 8.3 Testing before cutover, and phases

The new site is tested in four layers, ending with **shadow running**: it receives copies of the live site's real
inputs, does the same work on its own copy of the data, and its results are compared with the live site every day.
The live site stays in charge throughout, and **the shadow site can't charge anyone, email anyone or open anything**.

### Layer 1: automated tests (from the first day of building)
- Unit tests for the rules that matter most: membership status changes, subscription charges and billing, credit,
  door and machine decisions, fees and adjustments.
- The **front-door compatibility suite** (`tests/door_compat/`) with a backend for the new site: the real door
  firmware must pass with no expected failures.
- Every change runs the tests automatically (GitHub Actions) before it can be deployed.

### Layer 2: a staging site with test data
- A second App Platform app ("staging") with its own database, using anonymised copies of production data (made by
  the migration script, 8.2) and the `@example.com` fixtures.
- **GoCardless sandbox** and **Stripe test mode**, so the full payment flows (set-up, charge, confirmed, failed,
  mandate cancelled) can be run end to end with fake money.
- Email goes to a test mailbox, never to members. Moodle is a test copy.
- Volunteers and trustees try it out here: joining, paying, booking inductions, the admin screens, the reports.

### Layer 3: shadow running on a nightly copy of the live data
- Each night, a job takes a dump of the live database, runs the migration script and loads it into a **shadow**
  copy of the new site. This is real member data, so the shadow site is secured like production (private, admins
  only, in the same DigitalOcean account).
- **Once the new site is live, proofs of identity (the `IdDocument` table, 03 §3.1) are never copied**: any dump of
  the new site's database for staging, shadow or development leaves that table's rows out.
- The shadow runs its own scheduled jobs in **dry-run mode**: it works out which charges it **would** create and
  bill, and which members it **would** suspend or mark as left, but sends nothing to GoCardless and no emails.
- It talks to GoCardless and Stripe only with **read-only API keys**, so it can fetch events, payments and payouts
  (also to test reconciliation, 04 §4.9) but can't move money.

### Layer 4: mirroring the live site's inputs into the shadow site
- **Device traffic** (the front door and the laser): the live server's nginx (1.15.8, which has the built-in `mirror`
  feature) sends a **copy** of every `/acs/*` request to the shadow site. The live site still answers the device;
  the shadow's answer is logged and thrown away. Short timeouts on the mirror (about 1 second) mean a slow or broken
  shadow site can't delay the door. ⚠️ Test the mirror on the clone first, including with the shadow site down.
- **GoCardless webhooks:** mirrored the same way (or GoCardless sends to a second endpoint), so the shadow processes
  the same payment events as the live site.
- **Web pages:** not mirrored. Copying members' form submissions would make the shadow do things twice. Instead
  people use the staging site (layer 2), and the shadow's pages can be browsed read-only by admins to check that
  each member's page shows the same as the live site.
- Because the shadow's data is a nightly copy, things that changed during the day (for example a fob issued at
  lunchtime) can differ until the next copy. The comparison report marks these.

### The daily comparison report
Emailed to the build team each morning, after the live billing (01:30) and membership checks (06:00):
- **Door and machine decisions:** every mirrored tap, live answer vs shadow answer (allowed, refused, checked in).
- **Billing:** the charges and Direct Debits the live site actually created vs those the shadow would have created.
- **Members:** status, expiry date and payment method for every member.
- **Credit:** each member's calculated balance vs the live `cash_balance`.
- **GoCardless events:** how each was handled on both sides.
Every difference is either fixed or explained. Cutover happens after a run of days (suggested: two full billing
cycles, about 8 weeks) with no unexplained differences.

### Phases
1. **Build and test** (layers 1 and 2).
2. **Shadow running** (layers 3 and 4) until the daily report is clean.
3. **Device switch.** Point the devices (or a proxy in front of `/acs/*`) at the new system. The v1 addresses and
   replies are identical, so this is a quick switch with an instant way back. New-firmware (v2) devices can then be
   fitted one at a time.
4. **Member switch.** Put the current system into maintenance mode, run the final migration, point the web address
   and GoCardless at the new system, **turn off the old nightly jobs** and turn on the new ones. Switch Moodle to
   "Log in with BBMS" the same night (rehearsed beforehand on a copy of Moodle), then remove Moodle's database
   access. Do this **after the 01:30 billing run and before the next one**.
5. **Retire.** Keep the old database read-only for at least a year for reference and audit, then archive it.

## 8.4 Acceptance checks before cutover

- For every member: status, `active`, expiry date, payment method and credit balance **match** legacy (or differences are explained).
- For every active fob: door decision and every machine decision **match** legacy.
- The front-door compatibility suite (`tests/door_compat/`) passes against the new system **with no expected failures**
  (`DOOR_COMPAT_STRICT=1`). It runs the real door firmware against the server.
- A test GoCardless sandbox run: set-up → charge → confirmed → failed → mandate cancelled behaves as in 03/04.
- Nightly jobs re-run twice in a row produce **no extra** charges or payments (idempotency).
- Restore from backup tested.
- Moodle: "Log in with BBMS" works for a test member, an existing Moodle account is linked (not duplicated), and
  a completed test course shows as completed in BBMS.
