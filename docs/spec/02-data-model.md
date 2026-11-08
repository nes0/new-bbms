# 2. Data model

Legacy table names are shown in `code`. "New" notes describe how the rewrite should differ.
All money in the new system is **integer pence**; all timestamps are timezone-aware (Europe/London for display).

## 2.1 Entity overview

```
User ─┬─< KeyFob ─┬─< AccessLog (door / status events)
      │           └─< EquipmentSession (equipment_log)
      ├─< Payment
      ├─< SubscriptionCharge
      ├─< Induction >── Equipment.induction_category
      ├─< LockerRental >── Locker
      ├─< ProposalVote >── Proposal
      ├─< Expense
      ├─< Notification
      ├── ProfileData (1:1)
      ├─< Address (pending + approved)
      └─>< Role (role_user)
Equipment ── managed by Role; optional permaloan User
AcsNode (devices)   Settings (key/value)   AuditLog
```

## 2.2 Users (`users`)

| Field | Meaning / rules | New |
|---|---|---|
| `given_name`, `family_name` | Required. | |
| `email` | Required, unique. Login identifier. | |
| `secondary_email` | Optional, unique. Used to match PayPal payments. | Keep, shown as **PayPal email** (04 §4.4). **New:** `paypal_subscription_id` (PayPal's `subscr_id`), stored the first time a PayPal payment is matched |
| `email_verified` | Set via link `account/confirm-email/{id}/{hash}`. | Use framework's verification |
| `hash` | Random 30-char string. Used for email confirmation and **photo file names**. | Replace with signed tokens; photo keys use random UUID |
| `password` | bcrypt. Min 8 chars. | Framework default (argon2/bcrypt) |
| `phone` | Required (min 10) for self-registration; optional for admins. | |
| `emergency_contact` | Required for self-registration. Free text. | |
| `address_*` | **Legacy duplicate** of `user_address`; unused by current code. ⚠️ Verify | Drop |
| `notes` | Admin notes. | Admin-only |
| `status` | See 03 §3.2. | Enum |
| `active` | Boolean "has access to the space". Kept in sync with status. | Derive from status ❓ |
| `founder` | Display flag. | |
| `trusted` | Granted by admin. Prereq for keyholder. Triggers email + notification. | |
| `key_holder` | Granted by admin to a trusted member with a photo, a fob and a physical key (key deposit paid). | |
| `key_deposit_payment_id` | Payment that paid the £15 door-key deposit. | Keep as the **key deposit** (physical key). **New:** key issued date, key number (optional), returned date |
| `storage_box_payment_id` | Legacy; storage now derived from payments. | Drop |
| `induction_completed` | Member completed the online general induction / agreed rules. | |
| `inducted_by` | User id of the `comms` member who confirmed the general induction. | |
| `rules_agreed` | Timestamp the rules were accepted. | |
| `payment_method` | `gocardless-variable`, `balance`, `standing-order`, `paypal`, `gocardless` (old), `cash`, `''`. | Enum: `gocardless-variable`, `balance`, `standing-order`, `paypal`, `cash`, none |
| `secondary_payment_method` | Backup when `balance` is insufficient (only `gocardless-variable`). | |
| `payment_day` | 1–28 (values > 28 become 1). | |
| `monthly_subscription` | **Whole pounds**. Min 20 self-service / 5 admin. | Store pence; minimums are settings |
| `subscription_id` | GoCardless subscription id (old method). | **Drop** (old method dropped) |
| `mandate_id` | GoCardless mandate id (variable method). | |
| `gocardless_setup_id` | Transient during setup. | |
| `last_subscription_payment` | Legacy, unused. ⚠️ Verify | Drop |
| `subscription_expires` | Date membership is paid until. Central to the status rules. | |
| `cash_balance` | **Pence**. Cached member credit; recomputed from payments. | **Drop**: credit is calculated from the ledger when needed (04 §4.7) |
| `profile_private` | Hide from public member list. | |
| `banned`, `banned_date`, `banned_reason` | Banned users are refused every role-protected page. | |
| `slack_username` | Unused (Slack disabled). | Drop |
| (new) `discord_user_id`, `discord_username`, `discord_linked_at`, `discord_on_server`, `discord_welcome_due`, `discord_exempt` | The member's linked Discord account (06 §6.14). The id is unique. | |
| `import_match_string` | Text to match this member in bank-statement imports. | Keep if statement import kept |

## 2.3 Related member data

- **`profile_data`** (1:1): social links (twitter, facebook, google_plus, github, irc, website), `tagline`, `description`, `skills_array` (JSON list of skill keys), and photo flags:
  `profile_photo` (approved photo exists), `new_profile_photo` (awaiting approval), `profile_photo_private` (only show to members), `profile_photo_on_wall` (physical photo on the wall — counts instead of an online photo for door access).
- **`user_address`**: `line_1..4`, `postcode`, `approved`. A member edit creates an **unapproved** row; an admin approves/declines it. Admin edits are approved immediately.
- **`notifications`**: in-app messages `message`, `type`, `hash` (dedupe key, e.g. `trusted_status`, `<expenseId>-expense_approved`), `unread`, `notified_at/method`. **Built** (`apps/notifications`): `member`, `kind` (legacy `type`), `message`, `url`, `key` (legacy `hash`), `unread`, `created_at`; one per member and key. Legacy's `notified_method` is dropped (nothing was ever sent another way).
- **New: `IdDocument`** (proof of identity, 03 §3.1): `member`, `kind` (`photo_id` / `proof_of_address`, one of
  each), `filename`, `content_type` (from the file's contents), `size`, `data` (the file itself), `uploaded_at`.
  Deleted once checked, or after 30 days. Not in the admin; never migrated or copied to staging/shadow.
- **New: `IdCheck`** (kept permanently): `member`, `result` (`approved` / `rejected`), `note`, `documents_seen`,
  `checked_by`, `checked_at`. The record that ID was checked, holding no copy of the documents.
- **`audit_log`**: `admin_id`, `user_id`, `action`, `description`. Written when audited fields (`induction_completed`, `trusted`, `key_holder`) change.
  **New** (`MemberChange`): member, field, old value, new value, changed by, changed at; also logs the photo flags,
  banned and who confirmed the general induction (03 §3.9). Members also gain `inducted_at` (05 §5.9).
- **New: `MemberPhoto`** (06 §6.1): member, kind (`current` / `pending`, one of each), image (500 px PNG), thumbnail
  (200 px PNG), uploaded at. Files in private storage (Spaces) under random names; deleting the row deletes them.

## 2.4 Money

- **`payments`** — the **ledger**. One row per money movement.
  - `reason`: what it's for — `subscription`, `induction`, `door-key` (legacy; new: `key-deposit`), `storage-box` (box fee), `locker-rent` (new), `balance` (credit top-up), `equipment-fee`, `withdrawal`, `donation`, `transfer`, `costs`, or a consumable service name (e.g. printer).
  - `source`: where the money came from — `gocardless`, `gocardless-variable`, `stripe`, `paypal`, `cash`, `standing-order`, `balance` (paid from member credit), `reimbursement` (approved expense), `transfer`.
  - `source_id`: provider id (GoCardless payment id, Stripe charge id, PayPal txn id, `cash:<ts>`). `(source, source_id, user_id)` is used for de-duplication.
  - `reference`: context — subscription charge id, induction key, `<sessionId>:<device>` for equipment fees, expense id, etc.
  - `amount`, `fee`, `amount_minus_fee` — **pounds, float** in legacy.
  - `status`: `pending`, `paid`, `failed`, `cancelled`, `withdrawn`, … (GoCardless statuses are passed through).
  - `paid_at`.
- **New** (04 §4.9, 06 §6.13): `gocardless_payouts` and `stripe_payouts` (id, amount, fees, arrival date) with
  their line items linked to payments; bank statement lines (date, amount, description, matched to); member
  **status history** (member, from, to, when, by); a daily **membership snapshot** (date, counts by status).
- **`subscription_charge`** — one row per member per billing month: `charge_date`, `payment_date`, `amount` (pence, filled when paid), `status` ∈ `pending → due → processing → paid`, or `failed` (new: GoCardless gave up), or `cancelled`.

## 2.5 Access control

- **`key_fobs`**: `user_id`, `key_id` (RFID tag string, legacy ones prefixed `BB`, max 12 chars), `active`, `lost`. Marking lost deactivates.
  **New:** store the full hex tag *and* the decimal card number (the last 8 hex digits of the 10, as a number),
  each unique, so a device can identify a fob by either. Format confirmed with a real fob; example (made up): `700012d68733` → card 0001234567 (05 §5.1).
- **`acs_nodes`**: `name`, `device_id` (matches equipment `slug` for equipment nodes), `api_key`, `entry_device` (door vs equipment; **new:** a `role` of `entry`, `exit` or `equipment`), `monitor_heartbeat`, `last_boot`, `last_heartbeat`, `queued_command` (one pending command delivered on next check-in).
- **New** (05 §5.1–5.2): `fob_enrolments` (reader, member, opened by, opens at, expires at, captured card number and
  hex id, confirmed by/at); access log events gain a `kind`: `entry`, `check-in`, `exit`, `refused`.
- **`access_log`** (`Activity`): `user_id`, `key_fob_id`, `service` (e.g. `main-door`, `status`, device id), `response` (e.g. 402 = refused), `delayed` (event reported late by the device).
- **`equipment_log`** (usage sessions): `user_id`, `key_fob_id`, `device` (equipment slug), `active`, `started`, `last_update` (heartbeat), `finished`, `removed` (≤ 60 s, ignored), `billed`, `processed`, `reason` (`''`, `training`, `testing`), `notes`.

## 2.6 Equipment & inductions

- **`equipment`**: `name`, `slug` (unique, used as device key and induction key), `manufacturer`, `model_number`, `serial_number`, `colour`, `room`, `detail`, `description`, `help_text` (markdown), `ppe` (JSON list), `managing_role_id`, `requires_induction`, `induction_category` (groups machines sharing one induction), `working`, `permaloan` + `permaloan_user_id`, `access_fee` (**whole pounds** induction fee), `usage_cost` (**pence per hour**), `usage_cost_per`, `photos` (JSON list of S3 keys), `archive`, `asset_tag_id`, `obtained_at`, `removed_at`.
- **New** (05 §5.4, equipment readers): equipment status history (status, note, reported by, when); `meter_readings`
  (equipment, seq, active seconds, session, received at; unique per equipment + seq); `equipment_services` (date, note,
  by); per-equipment admin settings `meter_mode` (`current` or `session`), `current_threshold_amps` (current mode only),
  `idle_auto_end_minutes` (empty = off; current mode only) and `service_interval_hours` (empty = no reminders); sessions gain `ended_by`
  (`tap`, `button`, `idle`, `tidy-up`, `admin`), `original_started` / `original_finished`, `adjusted_by`, `adjusted_reason`, `motor_on_seconds`, `charged_at`, and reason `maintenance`.
- **New** (06 §6.12): `equipment.moodle_course_id` (optional), `equipment.course_required_for_existing` (flag),
  `members.moodle_user_id` (found via web services), and `training_completions` (member, Moodle course id,
  completed at, last checked, cleared by/at).
- **`inductions`**: `user_id`, `key` (= `induction_category`), `paid`, `payment_id`, `trained` (timestamp), `trainer_user_id`, `is_trainer`, `active`.

## 2.7 Other

- **`storage_boxes`**: `size` (volume units), `user_id` (0 = free), `active`. Boxes have no name or location, only an id.
  Boxes are now all 19 L; the volume field is obsolete.
  **Not migrated:** boxes are no longer tracked individually (06 §6.2).
  **New** (06 §6.2; lockers built 2026-11-07 in `apps/storage`): on members, a **box paid** flag + date + payment
  (to build); `StorageLocation` (name, description, order); `LockerSize` (name, description, order); `LockerRent`
  (location, size, default monthly rent in pence; one per location and size); `Locker` (location, number, size,
  availability: available / reserved for trustees / out of use + note, advertised rent override in pence + reason,
  retired; "rented" is worked out from an open rental); `LockerRental` (locker, member, started, started by, rent
  override in pence + reason, ended, ended by, end reason; at most one open rental per locker);
  `MemberLockerLimit` (member, limit, reason, set by, set at); `LockerChange` (the audit log); and later
  `locker_rent_charges` (rental, charge date, amount, status, like subscription charges). Location + number is unique
  for lockers.
- **`proposals`**: `title`, `description` (markdown), `user_id`, `start_date`, `end_date`, results (`votes_cast`, `votes_for`, `votes_against`, `abstentions`, `quorum`, `result`, `processed`).
- **`proposal_votes`**: `proposal_id`, `user_id`, `vote` (+1/0/−1), `abstain`.
- **`expenses`**: `user_id`, `category`, `description`, `amount` (**pence**), `expense_date`, `file` (receipt on S3), `approved`, `declined`, `approved_by_user`.
- **`settings`**: key/value. Legacy's only key, `emergency_door_key_storage_pin`, is defunct and is not migrated. See §2.8 for the new system's settings.
- **`roles`**, **`role_user`**: see 01 §1.3.
- **`detected_devices`**: `type`, `mac_address`, `display_name`. **Dropped**; not migrated.

## 2.8 Settings (new system)

Values that legacy hard-coded become admin-editable settings, with every change audit-logged.

**How they're stored (built 2026-11-07, `apps/site_settings`):** a single settings row with **one typed field per
setting** (rather than one row per setting), so each value is validated and has a label and help text in the admin.
Admins edit it at Admin → Settings; money is entered in pounds and stored in pence. Every change is recorded
(setting, old value, new value, who, when) in a read-only "setting changes" log. A setting is added by giving the
row a new field whose default is the starting value.
- Values are read from the database each time they're used (one quick query), so a change applies at once on every
  server, with nothing to restart. Each has limits checked when it's saved (for example a reader can't be told to
  check in more often than once a minute).
- **A setting is added when something uses it**, so the admin page never shows values that do nothing. The rows below
  marked **Built** are settings now; the others arrive with their feature (lockers, occupancy, billing and so on).
- Changing a price affects **new** payments only; nothing already recorded changes.

| Setting | Legacy value |
|---|---|
| Key deposit (physical key for 24-hour access; legacy door-key deposit) | £15. **Built** |
| Active fobs per member (fobs are free) | 2, 1–10. **Built** |
| How long a reader waits for a new fob to be tapped | new; 60 seconds (05 §5.1), 15 seconds–10 minutes. **Built** |
| Occupancy timeout (no tap-out) | new; default 4 hours after last fob activity, everyone cleared at 4am |
| Public occupancy count on the website | new; on/off |
| Attendance threshold for the trusted-status review list | new; ❓ suggested 8 visits over 4 weeks (05 §5.10) |
| Box fee (one-off, non-refundable) | £15 (charged outside BBMS today). **Built** |
| Lockers allowed per member (admins can override per member) | new; ❓ suggested 1, so it starts at 1 (0–20). **Built** |
| Locker rents | a default per **size and location**, in Storage → Default rents (06 §6.2), not on this page. **Built** |
| Minimum monthly subscription (self-service) | £20; **£25 in the new system** (decided 2026-11-07). Built. Members already paying less keep their amount until they change it |
| Minimum monthly subscription (set by admin) | £5. **Built** |
| Unchecked proofs of identity are deleted after | new; 30 days (03 §3.1), 1–90 days. **Built** |
| Suspension before a member is marked left | new; 30 days (decided provisionally). **Built** |
| Failed Direct Debit retries | new; "BBMS retries" (default) or "let GoCardless retry" (Success+); BBMS mode: retries (default 1) and days between (default 5). **Built** |
| Grace period per payment method | DD 10 days, standing order 6 months (183 days), other 7 days (03 §3.5). **Built** (PayPal uses "other": 7 days, as legacy) |
| Minimum billable equipment session | 15 minutes. **Built**. Readers show it, so changing it bumps every machine's `config_version` (09 §9.4) |
| Allowed negative credit for equipment fees | £5. **Built**, as "credit below zero still allowed to start a machine session": a member can start a charged session while their credit is above −£5. A session that has started is always charged in full (decided 2026-11-07, 04 §4.1) |
| How often v2 readers check in | new; 5 minutes (05 §5.4: also how quickly a revoked fob reaches readers), 1–60 minutes. **Built** |
| How often machine readers send a session heartbeat | new; 60 seconds (09 §9.5), 15 seconds–10 minutes. **Built** |
| Trustees' contact email | `trustees@buildbrighton.com` (arrives with the first email to the trustees) |
| Discord welcome message | new; posted when BBMS adds a member to the server (06 §6.14). **Built** |
| Space name (used in emails and pages) | "Build Brighton". **Built**; replaces the `SITE_NAME` environment variable |

## 2.9 Files

Stored on S3 (bucket per environment):
- Member photos: `<env>/user-photo/<md5(hash)>.png` + thumbnail (500px / 200px square crops), pending ones under a `new` prefix until approved.
- Equipment photos: under `equipment.getPhotoBasePath()`.
- Expense receipts: `<env>/expenses/<random>.<ext>`.

**New:** a private DigitalOcean Spaces bucket; serve through signed URLs or through the app with permission checks.
Member photos must not be guessable (md5 of a short value is guessable).

**Proofs of identity are the exception:** they are held in the database, not in Spaces, so there is one copy to
delete (03 §3.1, "Proof of identity").
