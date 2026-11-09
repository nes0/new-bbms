# Device API v2: reference for the firmware team

Base path `/api/devices/v2/`. JSON in and out, `snake_case` keys. Design background: spec 05 §5.4 and the build
brief, spec 09.

## Rules that apply everywhere

- **Auth:** `Authorization: Bearer <device key>`. Missing or wrong key: `401 {"error": "invalid_device_key"}`.
- **Money** is integer **pence**. **Times** are **unix seconds** (UTC), both ways.
- **The HTTP status describes the transport; the body describes the decision.** Every decision, allowed or refused,
  is `200` with a body. **Anything other than 200 means "BBMS couldn't decide": use your cache** (and for `304`, keep
  what you have).
  - `400 {"error", "detail"}`: malformed request (`bad_json`, `bad_card`, `bad_field`, `bad_event`, `bad_status`,
    `bad_request`, `too_many_events`). Don't retry unchanged.
  - `404 {"error": "session_not_found"}`: the session isn't on this device's machine.
  - `409 {"error": "wrong_device_role"}`: a machine endpoint called by a door reader. `{"error": "no_equipment"}`: a
    machine reader whose `device_id` matches no machine.
- **Retries are safe.** `end`, `meter` and `offline-events` are idempotent: resend after a timeout.
- Every request counts as a heartbeat.
- **Cards:** send `card` as the decimal card number printed on the fob (1-10 digits, leading zeros optional, e.g.
  `"0001234567"`) **or** the full 12-hex tag (`"700012d68733"`, either case). Anything else is `400 bad_card`.
  BBMS adds no vendor prefixes and applies no legacy rules.
- **Names** (`display_name`, `in_use_by`) are given name plus family-name initial ("Kim K."). What may be shown is an
  open trustee question.

## Suggested timings

| What | When |
|---|---|
| `check-in` | at boot, then every `heartbeat_seconds` (300) |
| session `heartbeat` | straight after a tap-in, then every `session_heartbeat_seconds` (60) |
| `meter` | every 60 s while the motor runs, and when a session ends |
| `allowed-fobs` | when check-in returns `refresh_cache` or a `cache_version` you don't hold |

The two intervals are admin settings in BBMS (300 and 60 to start with, limits 60–3600 and 15–600 seconds). Always
use the values from the latest check-in, and the machine's `minimum_minutes` from the latest `machine` object.

## `POST check-in`

```json
{"event": "boot", "firmware_version": "1.0.3", "cache_version": "9f2c41d0"}
```
`event` is `boot` or `heartbeat`; `cache_version` is `null` if you hold nothing.

```json
{"server_time": 1793914428, "heartbeat_seconds": 300, "session_heartbeat_seconds": 60,
 "cache_version": "9f2c41d0", "commands": ["refresh_cache"], "machine": {"...": "equipment readers only"}}
```
`commands` can contain `refresh_cache`, `purge_cache`, `reboot`, `disable`. Each admin-queued command is sent once.
`disable` is sent at every check-in while the device is disabled.

### The `machine` object
(also in `access`, `end` and `machine-status` responses)

```json
{"name": "Red laser", "config_version": 7, "hourly_rate_pence": 300, "minimum_minutes": 15,
 "status": "working", "status_note": "", "status_since": 1793900000,
 "in_use_by": "Kim K.", "session_id": 1234,
 "meter": {"mode": "current", "current_threshold_amps": "2.50"}}
```
`status` is `working` or `out_of_order`. `in_use_by` and `session_id` are `null` when idle. The threshold is a decimal
**string**, and `null` in `session` mode. `config_version` goes up whenever anything you show or use changes.

## `POST access`
`{"card": "0001234567"}`. Always `200` with `{"allowed", "reason", "display_name"}` (`display_name` is `null` for an
unknown fob). Door and exit readers also get `in_space`: how many are in the space after this tap.

**Door and exit readers**
| `reason` | `allowed` | Meaning |
|---|---|---|
| `ok` | true | open the door |
| `checked_out` | true | exit reader |
| `checked_in` | false | visit recorded; door stays shut |
| `not_member`, `not_keyholder`, `email_not_confirmed`, `no_induction`, `no_photo` | false | refused (`email_not_confirmed`: a key holder whose email address isn't confirmed; `no_induction`: one whose general induction isn't confirmed yet) |
| `unknown_fob`, `fob_lost` | false | |
| `enrolled` | false | an admin was waiting for a new fob on this reader and it has been captured for `display_name`; show "Fob registered" |
| `device_disabled` | false | |

**Machine readers**: `ok`, `in_use` (+ `in_use_by`), `not_member`, `out_of_order`, `not_trained`,
`course_incomplete`, `insufficient_credit`, `unknown_fob`, `fob_lost`, `enrolled`, `device_disabled`. All include
`machine`.
Allowed responses add:

```json
{"allowed": true, "reason": "ok", "display_name": "Kim K.", "session_id": 1234, "resumed": false,
 "session_reason": "", "balance_pence": -120, "hourly_rate_pence": 300, "machine": {"...": ""}}
```
`resumed: true` means that member already had the open session (you lost track, e.g. after a reboot).
`session_reason` is `maintenance` when a team member taps in on an out-of-order machine (no fee).

## `POST sessions/{id}/heartbeat`
Body `{}`. Open session:
`{"open": true, "elapsed_seconds": 754, "fee_so_far_pence": 75, "balance_pence": -75}`.
Already ended (200, not an error): `{"open": false, "ended_by": "tidy-up", ...the end summary}`; go back to idle.

## `POST sessions/{id}/end`
Tap-out `{"card": "..."}` or the end button `{"ended_by": "button"}`.
```json
{"ended": true, "already_ended": false, "duration_seconds": 3720, "fee_pence": 310, "removed": false,
 "session_reason": "", "balance_pence": -310, "machine": {"...": ""}}
```
If the card isn't the session owner's: `{"ended": false, "reason": "in_use", "in_use_by": "Kim K."}` (or
`unknown_fob`); keep the session going. `removed: true` means 60 seconds or less: free and hidden. Ending twice
returns the same summary with `already_ended: true`.

## `POST machine-status` (out-of-order button)
`{"status": "out_of_order" | "working", "session_id": 1234, "note": ""}`. Only works while that session is open.
Responses: `{"changed": true, "machine": {...}}`, `{"changed": false, "reason": "not_tapped_in"}`, or
`{"changed": false, "reason": "no_change"}`. The member's session carries on.

## `POST meter` (machines in `current` mode)
`{"seq": 4183, "active_seconds": 55, "session_id": 1234}` (`session_id` may be `null`; `active_seconds` 0-86400).
Responses: `{"accepted": true}`, `{"accepted": false, "reason": "duplicate"}` (same `seq` already stored; arriving
out of order is fine), `{"accepted": false, "reason": "session_metered"}` (that machine times tap-in instead; stop
sending). Keep `seq` increasing across reboots and resend unsent readings. If `idle_auto_end_minutes` is set, BBMS
ends a session whose motor has been still that long, at the last moment it ran.

## `GET allowed-fobs`
```json
{"cache_version": "9f2c41d0", "fobs": [{"card": "0001234567", "tag": "700012d68733", "display_name": "Kim K."}]}
```
Sorted by card number. `tag` may be `null`. Door and exit: keyholders who may open the door. Machines: members who
could start a session ignoring credit and machine status. Send `If-None-Match: "<cache_version>"` (the `ETag`) to get
`304`.

## `POST offline-events`
`{"events": [...]}`, at most 500 per call. Event `id` is yours: unique per device (e.g. boot counter + sequence).
```json
{"id": "a1b2-17", "type": "door", "card": "0001234567", "occurred_at": 1793910000, "allowed": true}
{"id": "a1b2-18", "type": "session", "card": "0001234567", "started_at": 1793910000, "ended_at": 1793913600,
 "ended_by": "tap", "motor_on_seconds": 3100}
```
Response: `{"results": [{"id": "a1b2-17", "result": "recorded"}]}` where result is `recorded`, `duplicate`,
`unknown_fob`, `overlap` or `invalid`. **Delete every event that has a result**; only a non-200 means try again.

## `GET qr`
A QR code for the device's page (raw bytes, `ETag`, `?format=json`). Format: spec 05 §5.4.

## `GET occupancy` (entry and exit readers)
Who's in the space now, for a screen by the door; machine readers get `409 wrong_device_role`.
```json
{"in_space": 2, "as_of": 1793910000, "version": "3be1f0a2",
 "people": [{"id": 12, "name": "Kim K.", "since": 1793908200, "using": ["Red laser"], "photo": "9c0d11e4"},
            {"id": null, "name": null, "since": 1793905000, "using": [], "photo": null}]}
```
Most recent arrival first. `since` is `null` for someone only known from a machine they're using. A person with
`id: null` has a private profile: show "a member". `photo` is `null` when there's no photo to show; otherwise it's the
photo's version, which changes when the member's photo does. Poll every 30-60 s with `If-None-Match: "<version>"`
(the `ETag`); `304` means nothing on the screen has changed.

## `GET photos/{id}` (entry and exit readers)
A person's photo from `occupancy`, square. `?size=16..200` (default 64), `?format=rgb565` (default), `png` or
`jpeg` (baseline). `rgb565`: bytes 0-1 width, 2-3 height (little-endian), then width × height pixels, each a
little-endian 16-bit RGB565 value, row by row from the top left (as an ESP32 holds a `uint16_t`; with TFT_eSPI's
`pushImage`, call `setSwapBytes(true)`). The `ETag` is `"<photo version>-<size>-<format>"`: keep the photo and send
`If-None-Match` to get `304`. `404 {"error": "no_photo"}` when the member isn't in the space now or has no photo that
may be shown; `400 bad_field` for a bad size or format.

## Open questions
- **Offline machine use:** may trained members start sessions while BBMS is unreachable, given credit can't be
  checked? The API supports either; the firmware decides whether to start sessions offline.
- **Display names:** what readers may show (trustees).
- **Card format:** which of decimal or hex the new readers give. Both work.
