# 9. Device API v2: build brief

This document is a **self-contained work order** for building the v2 device API (the API the new fob-reader firmware
will use). It's written so that a developer, or an AI agent in a fresh session, can pick it up without the history
of how it was decided.

- **What the API must do** is decided in [05 §5.4](05-access-control-and-equipment.md#54-device-api) ("v2: API
  for the new firmware" and "v2 for equipment readers") and [05 §5.6](05-access-control-and-equipment.md#56-usage-sessions-and-billing).
  That stays the source of truth. This brief turns it into exact requests, responses, models and tests, and makes the
  small design choices 05 leaves open. If you change a behaviour, update 05 as well.
- Written 2026-11-07, when the `equipment` slice (commit `ac7bc1b`) was the latest work.

## 9.1 Starting point

**Branch:** create `device-api-v2` from `equipment` (main doesn't have the equipment slice unless it has been
merged since). Commit only when the project owner asks.

**Run things** (from the repo root, with the virtualenv in `.venv`):

```bash
DEBUG=True .venv/bin/python manage.py test                       # all unit tests (93 at the start)
.venv/bin/ruff check . && .venv/bin/ruff format .                # lint and format (line length 120)
DEBUG=True .venv/bin/python manage.py makemigrations --check --dry-run
# Today's real front-door firmware against the new site; must stay 19/19 (needs the BBMSDoorEntry clone):
BBMS_BACKEND=django-local DOOR_COMPAT_STRICT=1 .venv/bin/python -u -m unittest tests/door_compat/test_door_compat.py
```

**What exists already:**

| Where | What you'll reuse |
|---|---|
| `apps/access/models.py` | `KeyFob` (`tag_id` 12-hex, `card_number` int, `active`, `lost_at`), `Device` (`device_id`, `role` entry/exit/equipment, hashed API key, `last_boot`, `last_heartbeat`), `AccessEvent` (door log, with `delayed`) |
| `apps/access/tags.py` | `normalise`, `card_number(tag)` (12-hex → int), `is_valid_full_tag`, `full_tag`, `format_card_number` |
| `apps/access/decisions.py` | `find_fob(raw_tag)`, `decide(device, raw_tag, occurred_at)` for door and exit readers (`Outcome` OPEN / CHECKED_IN / CHECKED_OUT / REFUSED + reason), `door_refusal_reason(member)` |
| `apps/access/api_v1.py` | The v1 views. **Don't change their behaviour**: the current door and laser depend on them. |
| `apps/access/api_v2.py` | `device_api_v2` decorator (Bearer or `ApiKey` header → `request.device`, else 401) and `GET qr`. Add the new views here, or in `apps/equipment/api_v2.py` for the machine-only ones. |
| `apps/equipment/rules.py` | `machine_refusal_reason`, `start_session(..., close_existing=False)` (pass False in v2), `heartbeat`, `end_session`, `session_fee_pence`, `open_session`, `is_on_team`, `tidy_up`, `member_credit_pence` (**stub: returns 0** until the payments app exists), `MINIMUM_CHARGE`, `UNCHARGED_REASONS` |
| `apps/equipment/device_api.py` | `equipment_for_device(device)` (device id = equipment slug) and the v1 machine handlers |
| `apps/equipment/models.py` | `Equipment` (`status`, `status_note`, `status_since`, `meter_mode`, `current_threshold_amps`, `idle_auto_end_minutes`, `service_interval_hours`, `usage_cost_pence_per_hour`, `team`), `UsageSession` (`motor_on_seconds`, `fee_pence`, `charged_at`, `ended_by`, `reason`, `removed`) |

Keep to the house style: plain functions for rules (in `rules.py`/`decisions.py`), thin views, comments that point at
the spec section, and tests named as plain-English sentences.

## 9.2 Scope

Build in this order; each phase is usable on its own and should end with green tests.

1. **Core equipment flow:** `check-in`, `access` (all three device roles), `sessions/{id}/heartbeat`,
   `sessions/{id}/end`. With these, a new machine reader can do everything today's laser reader does, plus live
   balance, hourly rate and in-use refusal.
2. **Machine extras:** `machine-status` (out-of-order button), `meter` (hour meter), idle auto-end, service records
   and reminders, running hours in the admin.
3. **Offline support:** `allowed-fobs` (with ETag), cache versions, device commands, `offline-events`.
4. **Firmware-facing reference:** `docs/device-api-v2.md`, written for the firmware team (see 9.10).

**Out of scope** (later slices; leave a `TODO(<app>)` where they plug in):
- charging fees to credit and the real balance: the payments app (`member_credit_pence` stays a stub);
- notifications to the machine's team: call one function, `notify_team(equipment, message)`, which now sends in-app
  notifications (06 §6.5);
- member-facing web pages for equipment (status note, service history); the Django admin is enough for now;
- the exit-reader occupancy display. (Fob enrolment windows, `reason: "enrolled"`, were built with the fobs slice.)

## 9.3 Conventions (all v2 endpoints)

- Base path `/api/devices/v2/`. JSON in and out, `snake_case` keys. Auth: `Authorization: Bearer <device key>`
  via the existing `device_api_v2` decorator. CSRF exempt (the decorator does it).
- **Money** is integer **pence**. **Times** sent to devices are **unix seconds** (integers, UTC): easy for a
  microcontroller. Times *from* devices are unix seconds too.
- **The HTTP status describes the transport; the body describes the decision** (05 §5.4). Every decision, allowed or
  refused, is `200` with a body. The device treats anything else as "BBMS couldn't decide" and uses its cache.
  - `400` `{"error": "<code>", "detail": "..."}`: malformed request (missing field, bad JSON, unknown `status` value).
  - `401` `{"error": "invalid_device_key"}`: from the decorator.
  - `404` `{"error": "session_not_found"}`: the session id isn't on this device's machine.
  - `409` `{"error": "wrong_device_role"}`: an equipment-only endpoint called by a door reader, or vice versa; and
    `{"error": "no_equipment"}` for an equipment reader whose `device_id` matches no (unarchived) equipment.
- **Retries are safe.** Firmware retries after timeouts, so `end`, `meter` and `offline-events` are idempotent (see
  each). Never return an error just because something already happened.
- Every request from a device updates `Device.last_heartbeat` (it proves the device is alive).
- Use `select_for_update()` on the `Equipment` row when starting or ending a session, so two taps arriving together
  can't both start a session.

### Cards

Requests carry `card`: **either** the decimal card number printed on the fob (up to 10 digits, leading zeros
optional) **or** the full 12-hex tag. Add `find_fob_by_card(card) -> (fob | None, reason)` in `decisions.py`:

| Input | Lookup |
|---|---|
| 12 characters, all hex (case-insensitive) | `find_fob(card)` (exact tag, then card number) |
| 1–10 digits | active fob with `card_number = int(card)` |
| anything else | `400 bad_card` |

If no active fob matches but an **inactive** fob with `lost_at` set matches, the reason is `fob_lost`; otherwise
`unknown_fob`. No "BB" legacy rule and no prefix guessing in v2.

### Display names

Add `Member.display_name` (a property) and use it everywhere v2 sends a name: for now **given name + first letter of
family name** ("Kim K."). What devices may show is an open trustee question (Discord and display names, trustees'
doc), so keep it in this one place. Leave the v1 `_user_payload` alone.

## 9.4 Data model changes

New fields and models (one migration per app per phase is fine):

**`access.Device`**
- `firmware_version` (char, blank): from check-in.
- `reported_cache_version` (char, blank): what the device says it holds; for the admin to see.
- `enabled` (bool, default True). When False, `access` answers `allowed: false, reason: "device_disabled"`, and
  check-in sends the `disable` command. This replaces nothing in v1.
- `pending_commands` (JSON list, default `[]`): commands queued by an admin (`refresh_cache`, `purge_cache`,
  `reboot`). Sent once at the next check-in, then cleared. Add admin actions to queue each.

**`equipment.Equipment`**
- `config_version` (positive int, default 1). Bumped (`F("config_version") + 1`) whenever something a reader shows or
  uses changes: `name`, `usage_cost_pence_per_hour`, `status`, `status_note`, `meter_mode`,
  `current_threshold_amps`, `idle_auto_end_minutes`. Do it in `Equipment.save()` by comparing with the stored row,
  so the admin, the API and any future page all bump it.

**`equipment.MachineStatusChange`**: the history behind the out-of-order button.
`equipment`, `status`, `note`, `reported_by` (member, nullable), `session` (nullable), `source`
(`button` / `admin`), `created_at`. Changing status in the admin also creates one (in `EquipmentAdmin.save_model`).

**`equipment.MeterReading`**: one per `POST meter`.
`device`, `equipment`, `session` (nullable), `seq` (positive big int), `active_seconds`, `received_at`.
Unique `(device, seq)`.

**`equipment.ServiceRecord`**: `equipment`, `serviced_at` (date), `recorded_by`, `note`, `created_at`. Inline on the
equipment admin.

**`equipment.OfflineEvent`** (phase 3): see 9.7.

**Running hours** are worked out, not stored (so they can't drift). In `rules.py`:
- `running_seconds(equipment, since=None, until=None)`: in **session** mode, the total duration of non-removed
  sessions (all reasons) in the range; in **current** mode, the sum of `MeterReading.active_seconds` received in the
  range.
- `hours_since_service(equipment)`: `running_seconds` since the latest `ServiceRecord`, or ever if none.
- `service_due(equipment)`: `service_interval_hours` is set and the hours since service ≥ that.
Show total hours, hours since service and "service due" as read-only fields on the equipment admin.

## 9.5 Phase 1: core endpoints

### `POST /api/devices/v2/check-in`

Request: `{"event": "boot" | "heartbeat", "firmware_version": "1.0.3", "cache_version": "abc123" | null}`

- `boot` sets `last_boot`; both set `last_heartbeat`, `firmware_version`, `reported_cache_version`.
- Unknown `event` → `400`.

Response (every role):

```json
{
  "server_time": 1793914428,
  "heartbeat_seconds": 300,
  "session_heartbeat_seconds": 60,
  "cache_version": "9f2c41d0",
  "commands": ["refresh_cache"],
  "machine": { ... }            // equipment readers only, see below
}
```

- `heartbeat_seconds` (300) and `session_heartbeat_seconds` (60) are admin settings (02 §2.8, built 2026-11-07), so
  firmware must use the values it's sent rather than assume these.
- `cache_version` is explained in 9.7. Until phase 3, return `""`.
- `commands`: the device's `pending_commands` (then clear them), plus `"disable"` if `enabled` is False, plus
  `"refresh_cache"` if the device's reported cache version differs from the current one (phase 3).
- `machine`, built by one function `machine_payload(equipment)` that `access`, `end` and `machine-status` also return:

```json
{
  "name": "Red laser",
  "config_version": 7,
  "hourly_rate_pence": 300,
  "minimum_minutes": 15,
  "status": "working",            // or "out_of_order"
  "status_note": "",
  "status_since": 1793900000,
  "in_use_by": "Kim K.",          // or null
  "session_id": 1234,             // the open session, or null
  "meter": {"mode": "current", "current_threshold_amps": "2.50"}   // threshold null in session mode
}
```

Send the threshold as a decimal **string** so no precision is lost; the firmware parses it.

### `POST /api/devices/v2/access`

Request: `{"card": "0001234567"}` (plus optional `occurred_at`; ignored for live taps, kept for symmetry).

Response always: `{"allowed": bool, "reason": "<code>", "display_name": "Kim K." | null, ...role-specific}`.

**Entry and exit readers:** call `decisions.decide(device, card, now)` after resolving the card with
`find_fob_by_card` (pass the fob's tag through, or refactor `decide` to accept a fob; keep v1 working). Map the
outcome:

| Outcome | Response |
|---|---|
| OPEN | `allowed: true, reason: "ok"` |
| CHECKED_IN | `allowed: false, reason: "checked_in"` (visit recorded; door stays shut) |
| CHECKED_OUT | `allowed: true, reason: "checked_out"` |
| REFUSED | `allowed: false, reason: <decision.reason>` (`not_member`, `not_keyholder`, `email_not_confirmed`, `no_induction`, `no_photo`) |
| no fob | `allowed: false, reason: "unknown_fob"` or `"fob_lost"`, `display_name: null` |

**Equipment readers**, in this order:
1. no fob → `unknown_fob` / `fob_lost`;
2. this member already has the **open session on this machine** (the reader lost track, e.g. after a reboot) →
   `allowed: true, reason: "ok"`, with that session (`resumed: true`);
3. **another member** has the open session → `allowed: false, reason: "in_use"`, `in_use_by: "<their display name>"`.
   Their session carries on;
4. `rules.machine_refusal_reason(member, equipment)` non-empty → `allowed: false` with that reason (`not_member`,
   `out_of_order`, `not_trained`, `course_incomplete`, `insufficient_credit`);
5. otherwise `rules.start_session(equipment, member, fob, close_existing=False)` → `allowed: true, reason: "ok"`.

Allowed equipment responses add:

```json
{
  "session_id": 1234,
  "resumed": false,
  "session_reason": "",           // "maintenance" when a team member taps in on an out-of-order machine
  "balance_pence": -120,
  "hourly_rate_pence": 300,
  "machine": { ... }
}
```

Refused equipment responses add `"machine": {...}` too, so the idle screen stays current.

### `POST /api/devices/v2/sessions/{id}/heartbeat`

Request: `{}` (optionally `{"card": ...}`, ignored). The session must belong to this device's machine, else `404`.

- Open session: `rules.heartbeat(session)`, then respond:
  `{"open": true, "elapsed_seconds": 754, "fee_so_far_pence": 75, "balance_pence": -75}`.
  `fee_so_far_pence` = `rules.session_fee_pence` applied to the session as if it ended now (so at least the minimum
  charge; 0 for training/testing/maintenance). Add `rules.fee_so_far_pence(session, now)` rather than faking
  `ended_at`.
- Already ended: `{"open": false, "ended_by": "tidy-up", ...the end summary below}` with `200`, so the device goes
  back to idle. (v1 returned 400 here; v2 doesn't, because it's a fact, not an error.)

**Balance rule** (use one function, `rules.live_balance_pence(member, session=None, now=None)`):
`member_credit_pence(member)` minus the session's fee (so far, or final) **if it hasn't been charged yet**
(`charged_at` is null). When the payments app starts charging at session end, the balance stays right without
changing the API.

### `POST /api/devices/v2/sessions/{id}/end`

Request: `{"card": "..."}` for a tap-out, or `{"ended_by": "button"}` for the end button.

- `card` given and it's the session's member's fob → end with `ended_by = tap`.
- `card` given and it's **someone else's** (or unknown) → don't end; `200`
  `{"ended": false, "reason": "in_use", "in_use_by": "Kim K."}` (or `unknown_fob`). The device keeps the session.
- `ended_by: "button"` (no card) → end with `ended_by = button`.
- Neither → `400`.
- Already ended → `200` with the same summary and `"already_ended": true`. **Idempotent**: never end twice or
  recompute a charged fee.

Response:

```json
{
  "ended": true,
  "already_ended": false,
  "duration_seconds": 3720,
  "fee_pence": 310,
  "removed": false,               // 60 seconds or less: free and hidden
  "session_reason": "",
  "balance_pence": -310,
  "machine": { ... }
}
```

## 9.6 Phase 2: machine extras

### `POST /api/devices/v2/machine-status` (out-of-order button)

Request: `{"status": "out_of_order" | "working", "session_id": 1234, "note": ""}`.

- The session must be **open** and on this machine: only a tapped-in member can press it. Otherwise `200`
  `{"changed": false, "reason": "not_tapped_in"}`.
- Same status as now → `{"changed": false, "reason": "no_change"}` (idempotent).
- Otherwise update `Equipment.status`, `status_note` (the note, or `""`), `status_since`; create a
  `MachineStatusChange` (`source = button`, `reported_by` = the session's member); call
  `notify_team(equipment, ...)`. Respond `{"changed": true, "machine": {...}}`.
- The member's current session carries on unchanged (they tap out as usual). Any member who is tapped in may report
  it **out of order**; per 05 only a team member could be tapped in on an out-of-order machine, so marking it
  **working** is in practice a team action. No extra check needed, but write a test showing it.

### `POST /api/devices/v2/meter` (hour meter)

Request: `{"seq": 4183, "active_seconds": 55, "session_id": 1234 | null}`.

- Equipment readers only (`409` otherwise). `active_seconds` must be 0–86400 (else `400`).
- Machine in **session** mode → `200 {"accepted": false, "reason": "session_metered"}`, nothing stored.
- `(device, seq)` already stored → `200 {"accepted": false, "reason": "duplicate"}`. Out-of-order arrival is fine:
  only exact repeats are ignored.
- Otherwise store a `MeterReading`. If `session_id` is an open or recently ended session on this machine, add
  `active_seconds` to its `motor_on_seconds` (`F()` update). Respond `{"accepted": true}`.
- Then run the **idle auto-end** check for this machine (below).

**Idle auto-end** (05 §5.4, "Forgotten tap-outs"): only when `meter_mode == current` and `idle_auto_end_minutes` is
set. If the machine has an open session and no reading with `active_seconds > 0` has arrived for that many minutes
(counting from the later of the session start and the last such reading), end the session at **the last moment the
motor was running**: the `received_at` of the last reading with `active_seconds > 0` after the session started, or the
session start if there was none. `ended_by = idle`. Write it as `rules.auto_end_idle_sessions(now)` and call it both
from `meter` and from `tidy_up()` (so it also works if the reader goes quiet).

**Service reminders:** `ServiceRecord` inline in the equipment admin; when `service_due(equipment)` becomes true,
call `notify_team()` once. To send it only once, store `service_reminder_sent_at` on `Equipment`, cleared when a
service is recorded. Check from `tidy_up()`.

## 9.7 Phase 3: offline support

### `GET /api/devices/v2/allowed-fobs`

The fobs this device may accept while BBMS is unreachable:

```json
{"cache_version": "9f2c41d0", "fobs": [{"card": "0001234567", "tag": "700012d68733", "display_name": "Kim K."}]}
```

- `tag` is null for fobs enrolled from a decimal-only reader.
- **Entry:** active fobs of members for whom `door_refusal_reason(member) == ""` (keyholders who may open the door).
  **Exit:** the same as entry. **Equipment:** active fobs of members who'd pass `machine_refusal_reason` ignoring
  credit and machine status (add a parameter, e.g. `for_offline_cache=True`). Team members are included.
- Sorted by card number, so the version is stable.
- `ETag` = `"<cache_version>"`; `If-None-Match` matching → `304`.

**`cache_version`** = the first 8 hex characters of SHA-256 over the sorted list (cards + names). Computed on
demand, never stored, so it changes *automatically* whenever anyone is added or removed (fob lost, member left,
keyholder removed, training revoked) without signals to maintain. Check-in returns it, and adds `refresh_cache` to
`commands` when the device's reported version differs. This meets 05's revocation goal: devices notice within one
check-in (5 minutes). **Update 05 §5.4 "Revocation"** to say the version is a hash rather than a bumped counter.
With a few hundred members this is a cheap query; if it ever isn't, cache it for a minute.

### `POST /api/devices/v2/offline-events`

Request: `{"events": [ ... ]}`, up to 500 per call (else `400`). Each event:

```json
{"id": "a1b2-17", "type": "door", "card": "0001234567", "occurred_at": 1793910000, "allowed": true}
{"id": "a1b2-18", "type": "session", "card": "0001234567", "started_at": 1793910000, "ended_at": 1793913600,
 "ended_by": "tap", "motor_on_seconds": 3100}
```

- `id` is chosen by the device, unique per device (e.g. boot counter + sequence). Store each event as an
  `OfflineEvent` (`device`, `event_id`, `type`, `payload` JSON, `received_at`, `result`) with unique
  `(device, event_id)`. **Already stored → skip.** That makes resending safe.
- `door`: create an `AccessEvent` at `occurred_at`, `delayed = True`, kind `entry` if allowed else `refused`
  (reason `offline`). Don't re-judge it; record what happened.
- `session`: create a finished `UsageSession` with those times (`ended_by` from the event, default `device`) and work
  out its fee with `end_session(..., when=ended_at)`, so it's charged once payments exist. Skip if it would overlap
  an existing session on the machine (`result = "overlap"`).
- Unknown card → store with `result = "unknown_fob"`; don't fail the batch.
- Response: `{"results": [{"id": "a1b2-17", "result": "recorded" | "duplicate" | "unknown_fob" | "overlap" |
  "invalid"}]}`. The device deletes every event that has a result (all of them); only a non-200 means "try again".

❓ **Open (trustees and firmware team):** may trained members use machines offline, given credit can't be checked
(05 §5.4)? This API supports it either way; the firmware decides whether to start sessions offline. Note it in
`docs/device-api-v2.md`.

### Device commands

Admin actions on the device list: **Queue "refresh cache"**, **Queue "purge cache"**, **Queue "reboot"**, and
**Disable/Enable** (the `enabled` field). Show `firmware_version`, `last_boot`, `last_heartbeat`,
`reported_cache_version` and whether it matches the current one.

## 9.8 Trying it by hand: the fake device

`/dev/fake-device/` (only when `DEBUG` is on) is a pretend machine reader that already speaks this API: a 296 × 128
one-bit e-paper screen, the relay LED, a fob reader (type a card number or tag, then Tap), the red **Stop** and blue
**Fault** buttons, a slider for the machine's current and switches for the reader's power and for BBMS being
reachable. A request log shows every call and reply. Code: `templates/access/fake_device.html` and
`static/access/fake_device.js`.

```bash
DEBUG=True .venv/bin/python manage.py create_demo_device   # demo laser, its reader key and four demo fobs
DEBUG=True .venv/bin/python manage.py runserver            # then open http://localhost:8000/dev/fake-device/
```

It was written against this brief **before** the endpoints existed, so until phase 1 is built it shows "BBMS
unavailable" (check-in returns 404). Use it to check each phase end to end. If you change a request or response
shape here, change the fake device to match. What it does with each reply:

- boot → `GET qr` (cached in "flash" with the ETag) and `check-in`; check-in again every `heartbeat_seconds`;
- idle screen: machine name, rate, minimum charge, out-of-order banner and note, QR code; an open session found at
  check-in after a power cut shows "Session open: <name>", and Stop ends it with `ended_by: "button"`;
- tap when idle → `access`; when tapped in → `sessions/{id}/end` with the card;
- tapped in: session heartbeat straight away, then every `session_heartbeat_seconds`; between heartbeats it counts
  the fee and balance on locally;
- current mode: counts seconds above the threshold and sends `meter` every 60 s and when a session ends, with `seq`
  kept in flash and unsent readings resent;
- if `end` fails it turns the relay off anyway and retries every 15 s (offline events aren't simulated, nor is the
  offline fob cache).

## 9.9 Tests to write

Follow the style of `apps/access/tests/test_api_v1.py` and `apps/equipment/tests/` (reuse
`apps/equipment/tests/helpers.py`: `member()`, `laser()`, `trained()`). Use the test client with
`enforce_csrf_checks=True` and the `Authorization: Bearer` header. At least:

- **Cards:** decimal with and without leading zeros, 12-hex upper and lower case, a fob enrolled with only a card
  number, a lost fob gives `fob_lost`, rubbish gives `400`, the "BB" rule isn't applied.
- **Check-in:** boot vs heartbeat timestamps; firmware version stored; queued commands sent once then cleared;
  `disable` while disabled; machine payload present only for equipment readers; `config_version` goes up when the
  rate, status or threshold changes, and not when an unrelated field (e.g. `description`) changes.
- **Access, door:** keyholder `ok`; non-keyholder member `checked_in` with an `AccessEvent`; left member
  `not_member`; exit reader `checked_out`; disabled device `device_disabled`.
- **Access, equipment:** start; resume after a "reboot"; `in_use` with the other member's name and their session
  untouched; every refusal reason; team member on an out-of-order machine gets a `maintenance` session; two
  simultaneous starts can't both succeed (at least a test that the second sees `in_use`).
- **Heartbeat:** fee so far is at least the minimum; 0 for maintenance; balance = credit − fee so far (patch
  `member_credit_pence`); ended session gives `open: false`; another machine's session gives `404`.
- **End:** tap by owner; tap by someone else doesn't end; button; second end is idempotent and doesn't change the
  fee; ≤ 60 s session is `removed` and free.
- **Machine status:** report out of order (status, note, history row, `notify_team` called; patch it); report fixed;
  no change; not tapped in.
- **Meter:** accepted and added to the session; duplicate seq; session-metered machine ignores it; bad
  `active_seconds`; running hours in both modes; hours since service resets after a `ServiceRecord`; service
  reminder sent once.
- **Idle auto-end:** ends at the last running moment; doesn't trigger in session mode or when the option is off.
- **Allowed fobs:** contents per role; lost fob, left member and revoked training disappear and the cache version
  changes; `304` with a matching ETag.
- **Offline events:** door and session events recorded; resent batch gives `duplicate` and creates nothing new;
  unknown card and overlap don't fail the batch; over 500 events gives `400`.
- **Regression:** all existing tests and the door_compat suite still pass. v1 endpoints unchanged.

## 9.10 Done means

- All four phases built and tried on the fake device, `manage.py test` green, ruff clean, no missing migrations, door_compat 19/19 strict.
- `docs/device-api-v2.md` written for the firmware team: every endpoint with an example request and response, the
  card formats, the "non-200 means use your cache" rule, the retry/idempotency rules, the QR format (already in 05),
  suggested timings (check-in every `heartbeat_seconds`, session heartbeat every `session_heartbeat_seconds`, meter
  every 60 s while running), and the open questions (offline machine use, display names, which card format the new
  readers give).
- 05 §5.4 updated where this brief made a choice 05 didn't (cache version as a hash, idempotent end/heartbeat,
  `resumed`, `fob_lost`, `device_disabled`), and the README's status section updated.
- `TODO(payments app)`, `TODO(settings app)` and `TODO(notifications app)` left where those apps plug in.
