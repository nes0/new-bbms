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

**Before cutover:** move any member still on old-style GoCardless subscriptions to variable Direct Debit, and any
PayPal payers to Direct Debit or standing order (both methods are dropped). Records for dropped features (CCTV,
detected devices) aren't migrated.

### Import script status (2026-11-07)

`manage.py import_legacy` (code in `apps/legacy/`) reads the legacy MySQL with a read-only user and imports, into an
**empty** database: members (legacy passwords keep working), roles, key fobs, devices (keeping their API keys),
equipment, inductions, machine sessions and the door log. It prints a report with counts, skipped rows (by legacy
id) and things to check; it never shows names or emails. `--dry-run` imports, reports and rolls back.

Choices made in the script (change them there if the trustees decide otherwise):
- `payment-warning` → active (it still had access); `on-hold` → suspended. ❓
- Fobs that clash on tag or card number: the active, most recently changed one is kept; the rest are listed.
- The Member Box item and its inductions aren't imported; the members who qualify for the box-paid flag are listed
  (stored once the storage app exists).
- Every finished legacy session is marked as **settled**, so the new system never charges for it. Sessions the
  legacy system hadn't billed are counted in the report, with what they'd cost.
- Legacy `402` door entries become check-ins.
- Equipment charged per gram or per page has its usage cost set to 0 (per hour only), and is listed.

Not imported yet (waiting for their apps): payments, charges, addresses, proposals, expenses, notifications, the
audit log, photos and receipts. Tested on the legacy clone's data inside the Docker image (`tools/clone_test/`).

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
