# 5. Access control, equipment and inductions

## 5.1 Key fobs

- Admin issues a fob to a member by entering its RFID tag id: **unique, 8–12 characters**.
- **Who gets a fob** (decided 2026-11-07): **every new member, free, on their first visit** after joining. Before
  they're a keyholder, the fob lets them **tap in at the door** to record their visit (5.2) and use the machines
  they're trained on (5.3), for example during supervised sessions. Supervision is a matter of space policy;
  BBMS doesn't enforce it. ❓ Who may issue fobs? Suggested: admins and the `comms` role (who meet new members).
  ❓ Is a replacement for a lost fob also free? Suggested: yes, unless an admin decides otherwise.
- **24-hour access needs a physical key**, with a **£15 key deposit** (a setting; the legacy `door-key` deposit).
  See 5.2.
- A member may hold up to **2** active fobs (legacy limit; a setting).
- **New:** the space has a fob reader with a screen that can show a fob's id in **raw (hex)** or **decimal** form.
  When issuing a fob, the admin reads it in hex mode, so BBMS records the vendor prefix as well as the card number
  and nothing has to be guessed. If that reader works as a USB keyboard, the admin can simply tap the fob into the
  issue form.
- **Tag format (confirmed 2026-11-05 with a real fob):** `700012d68733` = vendor prefix `70` + card number `0012D687`
  (decimal **0001234567**, printed on the fob) + checksum `33` (XOR of the five bytes `70 00 12 D6 87`). So BBMS can
  derive the decimal card number and the checksum from the hex, and the migration can fill in every fob's card number.
- A member may have several active fobs. "Mark lost" deactivates a fob (it is never deleted, so history is kept).
- **Tag lookup tolerance** (devices send ids in different formats). A tag `T` is found by trying, in order:
  1. exact match on an active fob;
  2. `"BB" + T`, truncated to 12 characters (old-style fob ids);
  3. drop the first character of `T` and match fobs whose id *contains* the rest (partial match — new ACS API only).

  ⚠️ Rule 3 can match the wrong fob. **New:** BBMS stores each fob's full tag (when known) and its **card number**
  (unique). A lookup matches the full tag exactly, or, for a 12-hex tag with a valid checksum, its card number. No
  partial matching (see "Enrolling a new fob" below).

### Enrolling a new fob by tapping it on a reader (added 2026-11-07)
- On a member's admin page, next to "add fob", a **"Tap new fob on the door reader"** button opens an **enrolment
  window** (60 seconds, a setting) on a chosen reader (the front door by default; any reader can be used). Anyone who
  may issue fobs can use it (5.1).
- The **first tap of a fob that BBMS doesn't know**, on that reader, during the window, is captured. The admin page
  (which refreshes itself) shows the captured **card number** and, if the reader sent it, the full hex id. The admin
  checks it against the number printed on the fob and confirms; only then is the fob added to the member.
  Cancelling, or letting the window run out, captures nothing.
- **With today's door firmware (v1):** an unknown fob makes the door send up to 13 lookups with different guessed
  vendor prefixes. BBMS takes the card number from the first (it's the same in every guess), ignores the rest, and
  answers with a refusal, so the door stays shut and nothing is cached. The vendor prefix isn't known for fobs
  enrolled this way, so **v1 lookups also match on card number** (the 8 hex digits after the prefix, with a valid
  checksum), not only on the full tag. The door's first guess then matches, and the 13-prefix loop no longer
  happens for these fobs. Card numbers must be unique (5.1).
- **v2:** during an enrolment window the reader's `POST access` reply is `allowed: false, reason: "enrolled"`, with the
  member's name, so its screen can show "Fob registered".
- Only one enrolment window per reader at a time. Known fobs tapped during the window are handled normally. Each
  enrolment is audit-logged (who opened it, which reader, what was captured, who confirmed).
- Typing the id (or reading it on the reader with a screen) stays available.

## 5.2 Who may open the door

A fob opens the main door only if its member is **all** of:
1. `active` (has access to the space);
2. `trusted`;
3. `key_holder`;
4. has an **approved online photo** *or* a **photo on the wall**.

**Becoming a keyholder** (24-hour, unsupervised access): an admin grants key holder to a member who is trusted, has
an approved photo (or photo on the wall), has a fob, and has paid the **£15 key deposit**, and records the physical
key issued to them (date, and key number if keys are numbered; returned date when it comes back).
❓ Is the key deposit refunded (to credit) when the key is returned? Suggested: yes; it's kept if the key is lost.

**Non-keyholders tap in at the door** (decided 2026-11-07). The door doesn't open for them, but BBMS records the tap
as a **visit**, so the space can track footfall and members build up attendance towards trusted status (5.10).
- **v1 (today's door firmware):** BBMS still answers with a **4xx refusal**, so the door stays shut and the fob isn't
  cached, but when the tag matches an active fob of a current member BBMS logs it as a **check-in**, not a refusal.
  The door's wrong-prefix attempts (404, no matching fob) aren't logged. No firmware change is needed. The member
  sees the door's normal "refused" behaviour and no Discord post is made.
- **v2:** `POST access` from an entry device returns `allowed: false`, `reason: "checked_in"` (or `"not_keyholder"`
  for a member who isn't current), so new firmware can show "checked in" instead of "refused". The door's
  `allowed-fobs` list holds keyholders only.
- The legacy system already logs these taps as 402 "Not a keyholder" refusals, so migrated access logs give
  **historical** attendance (08).

Refusals are written to the access log with response 402 and a reason message ("Not a member", "Not a keyholder",
"Member not trusted"). Successful entries are logged as `main-door` activity.

❓ Trustees: legacy checks the `active` flag, not `status`. A `leaving` member keeps `active = true` until expiry
(correct). Proposal: express the rule as an explicit list of statuses: `active`, `leaving`, `payment-warning`, `honorary`.

(The legacy emergency key-safe PIN feature is defunct and is not rebuilt.)

### Exit reader (added 2026-11-07)
- A second reader by the door logs members **leaving**. Tapping out is a **courtesy**, never required; it makes the
  live occupancy count (06 §6.7) more reliable.
- Devices have a **role**: `entry` (the front door), `exit`, or `equipment`. An exit reader never opens anything.
- **v2:** `POST access` from an exit reader replies `allowed: true, reason: "checked_out"`, and BBMS logs an **exit**
  for the member. It's accepted for any current member with an active fob, keyholder or not.
- **v1:** today's door firmware opens its relay on any 2xx reply. An exit reader running that firmware gets a 2xx
  (so a member sees the success signal), which is harmless only if its relay isn't wired to a lock. Prefer v2
  firmware for the exit reader.
- An exit tap counts as fob activity for attendance (5.10), like any other tap.

## 5.3 Who may use a machine

A fob starts a session on a machine (identified by device id = equipment `slug`) only if:
1. the member is `active`;
2. the device id matches a piece of equipment;
3. if the equipment `requires_induction`: the member has a **paid** induction for it **with a trained date**;
   **and**, if the equipment has a Moodle course, BBMS has recorded the member as having **completed** it (06 §6.12);
4. the member's credit + £5 is **greater than zero** (i.e. credit > −£5).

Starting a session on a machine **closes any other open session on that machine** first.

## 5.4 Device API

New access-control firmware is planned, so the device API is being **redesigned now** (v2, below). Until every device
runs the new firmware, BBMS must also serve the **v1** endpoints that today's devices use, unchanged.

### v1: compatibility API (keep until the current devices are replaced)

Each device (`acs_nodes`) has an **API key** sent in the `ApiKey` header. All endpoints below require a valid key.

| Method & path | Purpose | Request | Response |
|---|---|---|---|
| `GET /acs/test` | Connectivity check | — | `["OK"]` |
| `POST /acs/node/boot` | Device booted | — | 200; updates `last_boot` |
| `POST /acs/node/heartbeat` | Still alive | — | 200; updates `last_heartbeat` |
| `GET /acs/status/{tagId}` | Who is this fob? | — | `{user: {id, name, status, active, key_holder, cash_balance, profile_private}}` |
| `POST /acs/activity` | Fob presented | `tagId`, `device`, `occurredAt` (unix time, optional) | **201** `{activityId, user:{…}}`; on refusal a 4xx. If the node is an **entry device** → door rules (5.2) + door activity logged. Otherwise → machine rules (5.3) + new usage session, `activityId` = session id. |
| `PUT /acs/activity/{id}` | Session still running | `tagId` | 200; updates `last_update` |
| `DELETE /acs/activity/{id}` | Session ended | `tagId` | 204; sets `finished`. 400 if already finished. |

- Keep these URLs and response shapes **exactly**. Confirmed for the front door (see "Front door controller" below,
  and the compatibility suite in `tests/door_compat/`). The only equipment with a working fob reader today is the **red (Trotec) laser** (confirmed by the trustees, 2026-11-06). ⚠️ Verify which of these endpoints it uses (its firmware, or the server logs).
- **Retired** (decided 2026-11-05, not rebuilt): `POST /access-control/main-door`, `/access-control/status`,
  `/access-control/device`, `POST /acs` (multiplexed endpoint and its queued commands), `POST /acs/spark`.
  Confirmed 2026-11-06: no printer-charging or CCTV device is still sending to BBMS.

### v2: API for the new firmware (proposal for the firmware team)

Goals: no vendor-prefix guessing, fast revocation, proper records of offline entries, remote cache control, and a
clear rule for "BBMS answered" versus "BBMS couldn't answer".

**Conventions**
- Base path `/api/devices/v2/`, HTTPS only, JSON. Auth: `Authorization: Bearer <device key>` (one key per device, rotatable by `acs` role holders).
- **The HTTP status describes the transport, the body describes the decision.** A decision (allowed *or* refused) is
  always `200` with `"allowed": true|false` and a machine-readable `reason`. Anything that isn't a `200` with a valid
  body means "BBMS couldn't decide": the device falls back to its cache. That keeps a refusal and a failure apart.
- Fobs are identified by the **card number exactly as the reader gives it** (`"card": "0012345601"`). BBMS resolves
  it, so devices need no vendor-prefix list. BBMS stores each fob's card number separately, and refuses to issue a
  second fob with the same card number.

| Method & path | Purpose |
|---|---|
| `POST check-in` | Boot and heartbeat in one. Body: `{event, firmware_version, cache_version}`, where `event` is `boot` or `heartbeat`. Response: `{server_time, heartbeat_seconds, cache_version, commands: [...]}`. Commands include `refresh_cache`, `purge_cache`, `reboot`, `disable`, `enable`. |
| `GET allowed-fobs` | The full list this device may let in offline: `[{card, display_name}]`, with an `ETag`. Devices send `If-None-Match` and get `304` when nothing changed. Door: current keyholders. Equipment: members trained on it. |
| `POST access` | A fob was presented: `{card}`. Response: `{allowed, reason, display_name, session_id?}`. `reason` is one of `ok`, `checked_in` (door: visit recorded, door stays shut), `unknown_fob`, `fob_lost`, `not_member`, `not_keyholder`, `no_photo`, `not_trained`, `course_incomplete` (Moodle course not completed, 06 §6.12), `in_use` (another member is tapped in), `out_of_order`, `enrolled` (fob captured during an enrolment window), `checked_out` (exit reader), `insufficient_credit`, `device_disabled`. For equipment, `allowed` starts a usage session. |
| `POST sessions/{id}/heartbeat`, `POST sessions/{id}/end` | Equipment usage sessions (as v1 `PUT`/`DELETE /acs/activity/{id}`). |
| `POST offline-events` | Upload a batch of decisions the device made while BBMS was unreachable: `[{card, occurred_at, allowed, type}]`. BBMS records them in the access log as offline entries, at the time they happened. |
| `POST meter` | Equipment readers: motor-on seconds since the last report, with an increasing `seq` (see below). |
| `POST machine-status` | Equipment readers: a tapped-in member reports the machine out of order or fixed (see below). |

**Revocation:** when a fob is marked lost, or a member stops being allowed on a device (left, suspended, keyholder
removed, training revoked), BBMS bumps that device's `cache_version`. The device sees it at its next check-in and
re-fetches `allowed-fobs`. The heartbeat interval is set by the server (proposed: 5 minutes), so revocations reach
devices within minutes, and caches no longer hold fobs forever.

❓ For the firmware team and trustees:
- **Equipment offline policy**: credit can't be checked offline. Should trained members be allowed to use machines
  offline, with fees billed later from the uploaded events?
- **Display name**: what the device may show or announce (see Discord, below).
- **Card numbers**: readers that give the raw hex id (like the reader with a screen in the space) give the vendor
  prefix too, so `access` should accept **either** the decimal card number or the full hex tag. Firmware should
  prefer hex where the reader offers it. ⚠️ Confirm which formats the new door and equipment readers give.

### v2 for equipment readers (decided 2026-11-07)

The one machine reader in use today (on the red Trotec laser) has a screen showing the member's name and balance.
With new firmware it should show a **live** balance, show the **hourly rate before anyone taps in**, use
**tap to start, tap again to finish** (plus an **end button** for forgotten tap-outs), keep an **hour meter** of
the machine's motor-on time, and have a **second button** to report the machine **out of order** or **fixed**.

**Machine status feed.** The `check-in` response for an equipment device also carries the machine's details:
`{name, hourly_rate (pence), minimum_minutes, status: working | out_of_order, status_note, status_since,
in_use_by (display name, or null), session_id, meter: {mode: "current" | "session", current_threshold_amps}}`, plus a `config_version` so the device knows when to refresh
(it's also refreshed in every tap response). When idle, the screen shows the machine name, its hourly rate, and,
if it's out of order, that status and note.

**Sessions:**
- **First tap** (`POST access`): if allowed, a session starts. The response includes the member's display name,
  **balance** (pence), the hourly rate and the session id.
- **Same member taps again**: the session ends (`POST sessions/{id}/end` with the card). The response gives the
  duration, the **fee charged** and the **new balance**.
- **End button** (no fob needed): `POST sessions/{id}/end` with `ended_by: "button"`.
- **A different member taps while it's in use**: refused, `reason: "in_use"`, with the current user's display name.
  The current session carries on (decided 2026-11-07).
- **Live balance:** the device sends `POST sessions/{id}/heartbeat` every minute; the response gives the elapsed
  time, the **fee so far** (at least the minimum charge) and the **live balance** (balance − fee so far). Between
  heartbeats the device can count down locally from the hourly rate.
- **Charging happens when the session ends**, using **session time** (tap in to tap out, rounded up to the 15-minute
  minimum; decided 2026-11-07: not motor-on time). The member's credit changes straight away, so the next tap shows
  the right balance. The nightly 02:00 job (5.6) becomes a backstop for sessions closed by the hourly tidy-up.
  Reclassifying a session as training or testing still refunds the fee.
- **Running out of credit** (decided 2026-11-07): a session is **never stopped** for lack of credit, and the balance
  may go **negative** during it, with no lower limit. The credit check (above −£5, 5.3) applies only when a session
  starts. The screen shows the live balance, including when it's negative.
- **Forgotten tap-outs** (decided 2026-11-07): an **admin option per machine**, off unless an admin turns it on,
  and only available for machines metered by **current draw** (it needs to know the motor has stopped).
  When on, if the hour meter shows the motor has been off for the set number of minutes (suggested 30), BBMS ends
  the session at the last moment the motor was running.

**Hour meter:**
- **How each machine is metered is an admin option** (decided 2026-11-07), because some machines' running state can't
  be detected reliably from their current draw:
    - **Current draw** (`mode: "current"`): the reader measures the current in the machine's mains feed and counts time
      when it's **above a threshold**. The threshold (in amps) is set per machine in the admin panel and sent to the
      reader in the check-in reply (`meter.current_threshold_amps`); changing it bumps `config_version`, so readers
      pick it up within one check-in.
    - **Tapped-in time** (`mode: "session"`): running time is the time a member is tapped in. The reader doesn't
      need a current sensor and doesn't send meter readings; BBMS works it out from session start and end times.
- In current mode the reader reports `POST meter` with `{active_seconds (since its last report), seq (a counter
  that only goes up), session_id?}`. BBMS ignores repeated `seq` values, so a device can safely resend after an
  outage or reboot and lose nothing. Readings sent for a machine in session mode are ignored.
- BBMS keeps each machine's **total running hours**, running hours per month, and **hours since its last service**:
  the machine's team records a service (date and note), which resets that count. Each session also records its
  motor-on time, for statistics.
- **Service reminders** (decided 2026-11-07, nice to have): an admin can set a **service interval** per machine, in
  running hours. When hours since the last service reach it, the machine's team is notified.

**Out-of-order button:**
- A tapped-in member presses it to report the machine **out of order**, or, if it already is, **fixed**:
  `POST machine-status` with `{status, session_id}`. BBMS records who and when, notifies the machine's team (its
  managing role), and shows it on the equipment page, where the member can add a note. The team can also change the
  status on the equipment page.
- While a machine is out of order, ordinary members' taps are refused (`reason: "out_of_order"`). Members of the
  machine's team and admins can still tap in to test or fix it; their sessions are recorded as **maintenance** and
  not charged, and they press the button again to mark it fixed (decided 2026-11-07).

**Offline:** the device keeps the last machine status and hourly rate. Sessions it starts and ends while BBMS is
unreachable are uploaded through `offline-events` and charged when they arrive.

The current laser reader uses the v1 API (5.4), so all of this needs the new firmware; until then it carries on as
today. ⚠️ Verify which v1 endpoints it calls.

### Front door controller (confirmed from firmware)

Source: the `BBMSDoorEntry` repository (latest commit 2024-06-22). It's an OpenWrt router running a bash script
(`rfid-lock.bash.sh`), with a USB 125 kHz reader that types the card number as decimal digits. The door relay is driven
from a GPIO and held open for 5 seconds.

**How it talks to BBMS**
- `https://bbms.buildbrighton.com/acs/…` is **hard-coded**, and TLS is verified. The new system must answer on the
  same hostname with a valid certificate (or the script must be changed).
- Headers: `Content-Type: application/json`, `Accept: application/json`, `ApiKey: <key>`.
  The API key, device name and a Discord webhook URL are kept in `/root/keys` on the device (not in the repo).
- `POST /acs/node/boot` at start-up. `POST /acs/node/heartbeat` after **15 minutes** without a fob read (BBMS
  warns after 60). The device ignores the response body, so it **can't receive queued commands**.
- `POST /acs/activity` with a body of `{"tagId": "<12 hex>", "device": "<name>", "occurredAt": "<unix time now>"}`.
  It always sends `occurredAt`, so legacy BBMS marks **every** door entry as `delayed`.
- From the response it uses **only the HTTP status code and `user.name`**.

**How the status code is interpreted, which is safety-critical**

| BBMS response | Device reads it as | Legacy BBMS sends it when |
|---|---|---|
| `2xx` | allowed | entry allowed (`201`) |
| `4xx` | online, **refused**; the fob is removed from the cache | access rules fail (`422`), fob unknown or lost/inactive (`404`), unknown device (`404`), bad API key (`403`) |
| `5xx`, timeout or no connection | **server offline** | any unexpected error |

> **Requirement:** every refusal must return **4xx**. An unexpected error (500) makes the door treat BBMS as offline. The new system needs tests for this.

**Tag ids and vendor prefixes.** The reader gives only the card number in decimal, but BBMS stores the full 12-hex tag:
a 10-hex value (vendor prefix + card number) followed by a 2-hex XOR checksum. For a fob it hasn't seen before, the
script **tries up to 13 hard-coded vendor prefixes in turn** (`5500000000`, `BB00000000`, `1900000000`, …), one
`POST /acs/activity` each, until one gets a 2xx. The list is produced by `scrape.sh`, which logs in as an admin and
scrapes every member's account page.
- The new system must tolerate up to 13 rapid refusals from the door per tap: no lockout, and the wrong-prefix
  attempts shouldn't appear in the access log as real refusals.
- v2 removes the need for this: devices send the card number as read and BBMS resolves it.

**Offline cache behaviour**
- The cache is a file mapping decimal card number → full tag id (`/root/bbms-key-cache`). Fobs are added **only after
  a 2xx**, so this is learn-on-success. Names aren't stored.
- A fob is removed **only when it's presented while BBMS is online and BBMS refuses it (4xx)**.
- "Offline" means "the **previous** request failed".
- A new fob (never seen) can't get in during an outage.

**Requirements for the new system (until this door is replaced by v2 firmware)**
- Serve the v1 endpoints it uses byte-compatibly (URL, headers, status codes, `user.name`), and pass
  `tests/door_compat/` with no expected failures.
- Record v1 entries using BBMS's receive time, and only treat `occurredAt` as "delayed" when it's more than a minute or
  so in the past (legacy marks every door entry delayed).

**Discord announcements.** On every entry the door posts **"`<user.name>` is in the space!"** to a Discord channel
(a "public group", per the commit history). It uses the member's **full name**.
Offline entries are posted with a partially masked fob number instead.
❓ Trustees: should BBMS return a display name that respects privacy (e.g. first name only, or nothing for private
profiles), given that this field ends up in a public channel? This applies to `user.name` in v1 and `display_name`
in v2.

## 5.5 Device monitoring

- Devices with `monitor_heartbeat` set and no heartbeat for **over 1 hour** (checked every 15 minutes) trigger an in-app notification to every
  `acs` role holder (once per missed heartbeat).
- `acs` role holders manage devices (`/devices`): name, device id, entry vs equipment, monitor flag, API key.

## 5.6 Usage sessions and billing

Lifecycle of an `equipment_log` row: started → (heartbeats update `last_update`) → finished.

**Hourly clean-up (`fix-equipment-log`):**
1. Any session still active whose last heartbeat is over an hour old is closed at its last heartbeat time
   (or at start + 1 minute if it never sent one).
2. Back-to-back sessions by the same member on the same machine with the same reason, where the next starts
   **within 60 s** of the previous finishing, are merged into one.
3. Unbilled sessions lasting **≤ 60 s** are marked `removed` (not billed, hidden).

**Charging** (changed 2026-11-07): sessions are charged **when they end** (see "v2 for equipment readers" in 5.4).

**Nightly billing (`calculate-equipment-fees`, 02:00)**, now a backstop for sessions closed by the tidy-up: for each finished, unbilled, not-removed session on equipment
with a usage cost:
- duration is rounded **up to a minimum of 15 minutes**;
- fee = `usage_cost` (pence/hour) × duration, rounded to the penny;
- if the session has no reason (`training`/`testing`), an `equipment-fee` payment is taken from credit with
  reference `<sessionId>:<slug>`;
- the session is marked billed.

**Reclassifying a session:** a holder of the role named after the equipment (e.g. `laser`) or an admin can mark a session
as `training` or `testing` once. If it was already billed, the matching fee payment is deleted (refund).

**Adjusting a session's times** (added 2026-11-07), for example when a member forgot to tap out:
- An **admin** or a member of **the machine's team** (decided 2026-11-07) can change a session's start and end times,
  or end a session that's still open at a chosen time, giving a **reason**. Team members can only adjust sessions on
  their own team's machines.
- The new times must make sense: the end after the start, not in the future, and not overlapping another session on
  the same machine.
- BBMS works out the fee again from the new times (with the 15-minute minimum). If the session was already charged,
  the difference is a **new ledger entry**, a refund to credit or an extra charge, linked to the session. The original
  payment is never edited.
- The member gets a notification showing the old and new times and any change to the charge.
- The change is **audit-logged** (who, when, old and new times, reason), and the session keeps its original times
  for reference.
- For machines metered by tapped-in time, running hours follow the adjusted times. For machines metered by current
  draw, running hours come from the meter readings and don't change.

## 5.7 Equipment

- **Anyone logged in** can list and view equipment. ⚠️ Verify whether the list is also public.
- Holders of the `equipment` role can create, edit and archive equipment and upload or delete photos.
- The equipment page shows: details, photos, PPE, help text (markdown), whether it works, induction fee,
  usage cost, **trainers**, **trained members**, **members waiting for training**, usage history (finished,
  non-removed sessions), and total usage time split into billed, training and testing.
- The `managing_role_id` links equipment to a role (the "team" who looks after it).
- **Permaloan**: equipment on long-term loan from a member (`permaloan_user_id`), with a permaloan policy.

## 5.8 Equipment inductions (training)

1. Member pays the equipment's **access fee** (GoCardless, credit or Stripe). That creates an `inductions` row:
   `key` = induction category, `paid = true`.
2. They appear in that equipment's "waiting for training" list.
3. An **admin** marks them trained and chooses which trainer did it (`trained` = now, `trainer_user_id`).
   For equipment with a Moodle course, sign-off is **only possible once BBMS has recorded the course as completed**
   (decided 2026-11-07, 06 §6.12); the waiting list shows each member's course status.
   ❓ Trustees: legacy restricts this to admins. Should **trainers** be able to mark their own trainees?
4. An admin can promote a trained member to **trainer** for that category.
5. An unpaid-for or untrained induction can be **cancelled**, which refunds the fee to credit.

Several machines can share one `induction_category` (e.g. two drills), so one induction covers all of them.

## 5.9 General induction

Separate from equipment training: every new member reads the member agreement online, agrees to the rules and marks
the induction complete; a `comms` role holder then confirms it (`inducted_by`). See 03 §3.1.

## 5.10 Attendance (counts towards trusted status)

Added 2026-11-07. A **visit** is a calendar day (Europe/London) on which a member's fob was used: tapping in at the
door (keyholder entry or non-keyholder check-in) or starting a session on a machine. Several taps on one day count once.

- **Members** see their own attendance: number of visits, first and last visit, and recent visit dates.
- **Admins** see each member's attendance on their admin page, and a **trusted-status review list** of members who
  aren't trusted yet but meet the attendance threshold, for an admin to consider. Granting trusted status stays a
  manual admin decision.
- ❓ The threshold: suggested, a setting for "at least N visits over at least M weeks since joining" (e.g. 8 visits
  over 4 weeks). Should anything else count (inductions completed, training)?
- Attendance is derived from the access log and usage sessions; nothing extra is stored.
