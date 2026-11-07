# Front-door compatibility tests

These tests run the **real front-door firmware** (`BBMSDoorEntry/rfid-lock.bash.sh`) against a BBMS
server, feed it fob reads the way the USB reader does, and check what the door does.

The new BBMS must pass the `ServerContract` tests before the front door is pointed at it.

## Running

From the project root:

```bash
python3 -m unittest -v tests/door_compat/test_door_compat.py
```

No packages are needed (Python 3.10+ standard library only). The run takes a few minutes, because
refused fobs make the door try all 13 vendor prefixes.

| Variable | Default | Purpose |
|---|---|---|
| `BBMS_BACKEND` | `legacy-clone` | Which server to test (see `backends.py`) |
| `DOOR_FIRMWARE` | `BBMSDoorEntry/rfid-lock.bash.sh` | Firmware script to run |
| `BBMS_CLONE_SSH` | `ssh … root@<droplet-ip>` | How to reach the legacy clone |

## What is tested

**ServerContract** (requirements on BBMS):
- boot and heartbeat are accepted;
- a keyholder gets in, the fob is cached, and Discord gets the name;
- the request has the shape the firmware sends; vendor-prefix guessing works (wrong prefixes must be clean 4xx);
- a photo on the wall counts as a photo;
- refused fobs get **4xx, never 5xx**: non-keyholder, left member, no photo, lost fob, unknown fob, missing profile;
- a fob that is marked lost, or whose keyholder status is removed, is **removed from the door's cache** on the next tap.

**FirmwareBehaviour** (how today's firmware behaves in outages, so changes get noticed):
- when BBMS is unreachable, the first tap is refused and then cached fobs open the door;
- unknown fobs are refused during an outage;
- an HTTP 5xx is treated as an outage;
- after an outage, a cached fob is let in **before** BBMS is asked.

Tests marked `[differs on legacy: …]` are *expected* to fail on the legacy server and must pass on the new one.

## How it works

```
 fake USB reader ──stdin──▶ rfid-lock.bash.sh (sandbox copy) ──HTTP──▶ BbmsProxy ──HTTPS──▶ BBMS under test
                                     │                                   (can simulate down / 503)
                                     └──HTTP──▶ DiscordCapture
```

- The firmware is copied into a temporary folder and a few **named, exact substitutions** are made:
  server URL, reachability check, paths for keys, cache and GPIO, the 5-second door hold, the heartbeat
  interval, and a "ready" marker. If the firmware changes so that one of them no longer matches, the
  harness stops with `FirmwareChanged` and tells you what to update.
- The proxy speaks plain HTTP to the door and HTTPS to BBMS. So these tests **don't** check the door's TLS
  or hostname handling. The real door needs `bbms.buildbrighton.com` with a valid certificate.
- Test members and fobs are created by the backend with ids above 9000 and removed afterwards.

## Legacy clone notes

The clone can't reach SQS, Pusher or SMTP. Its `.env` therefore sets `QUEUE_DRIVER=sync`,
`BROADCAST_DRIVER=log` and `MAIL_DRIVER=log`. Without that, every successful door entry hangs while the app
tries to queue a broadcast event on SQS.

## Adding a backend for the new system

Implement the methods listed at the top of `backends.py` (create and remove fixtures, toggle a fob or
keyholder status, count recorded entries, read the device's last boot), add the class to `BACKENDS`, and run
with `BBMS_BACKEND=<name>`.
