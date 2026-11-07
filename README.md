# BBMS: Build Brighton Member System (rebuild)

The membership system for Build Brighton: members, payments, door and machine access, and more. This is the
**new Django version**, replacing the legacy Laravel/PHP system. What it must do is described in
[`docs/spec/`](docs/spec/README.md); start there.

**Status:** early. Built so far:

- members can log in (including with passwords migrated from the legacy system) and admins can manage them;
- online **registration** (`/register`) and proof-of-identity uploads checked by trustees (`/id-checks/`);
- the member's **account page** (`/account/`): membership, credit, fobs and training at a glance; editing their
  details, address (approved by a trustee), monthly amount and password; deleting an account that's still setting up;
- key fobs, devices and the v1 device API, so the current front-door firmware works unchanged, plus a QR-code call
  for reader screens;
- **issuing fobs** from a member's admin page: type or USB-read the fob's id, or **tap the new fob on any reader**
  (v1 or v2) and confirm the captured number. The 2-fob limit, mark lost and found again, and an audit log of
  enrolments;
- the **v2 device API** (`/api/devices/v2/`, built to [the brief](docs/spec/09-device-api-v2-build.md)): check-in,
  access for doors and machines, session heartbeat and end, out-of-order button, hour meter with idle auto-end and
  service reminders, offline fob lists and offline events. Reference for the firmware team:
  [docs/device-api-v2.md](docs/device-api-v2.md);
- a **fake machine reader** at `/dev/fake-device/` (local development only) that behaves like the planned new
  firmware and talks to BBMS through the v2 device API. Set it up with `manage.py create_demo_device`;
- equipment, inductions, Moodle course rules and machine usage sessions: fees, the hourly tidy-up
  (`manage.py tidy_equipment_sessions`) and session adjustments in the admin;
- **payments and member credit**: the ledger, credit worked out from it, machine fees taken from credit when a session
  ends (with corrections when a session changes), cash and manual payments in the admin, and the member's credit page
  (`/credit/`) with withdrawal requests;
- the **legacy import** (`manage.py import_legacy`), tested on the legacy clone's data inside the Docker image.

## What's in this repository

| Path | What it is |
|---|---|
| `bbms/` | Django project settings and top-level URLs |
| `apps/core/` | Site-wide pieces: home page, health check (`/healthz`), shared templates |
| `apps/members/` | Members and their login accounts, including the legacy password hasher |
| `apps/access/` | Key fobs, devices (door and machine readers), door decisions, the device API |
| `apps/equipment/` | Equipment, inductions, Moodle course completions, machine usage sessions and fees |
| `apps/site_settings/` | Settings the trustees change in the admin (prices, limits, reader timings, the space's name), with a log of changes |
| `apps/payments/` | The ledger, member credit, machine fees charged from credit, cash and manual payments, withdrawals |
| `templates/`, `static/` | Page templates; CSS and HTMX (kept locally, no build step) |
| `docs/spec/` | The specification |
| `tests/door_compat/` | Runs the real front-door firmware against a server (needs the `BBMSDoorEntry` clone) |
| `migration/` | Queries and scripts for moving data from the legacy system |
| `apps/legacy/` | The legacy import: `manage.py import_legacy` (spec 08 §8.2) |
| `tools/clone_test/` | How to build and run the Docker image on the legacy clone droplet, with its data imported |

Still to come (one app per area of the spec): subscriptions and billing (GoCardless, Stripe), storage, governance
(proposals, expenses), notifications, reports; and moving the remaining hard-coded prices and limits into settings.

## Getting started on your own computer

You need **Python 3.12**. Then, in this folder:

```bash
python3 -m venv .venv                 # make a private Python environment
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env                  # local settings (DEBUG on, SQLite database)
.venv/bin/python manage.py migrate    # create the local database (db.sqlite3)
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
```

Then open <http://127.0.0.1:8000>. The admin site is at <http://127.0.0.1:8000/admin/>.

(If you have [uv](https://docs.astral.sh/uv/), `uv venv` and `uv pip install -r requirements-dev.txt` are faster.)

## Checking your changes

```bash
.venv/bin/python manage.py test       # the test suite
.venv/bin/ruff check .                # lint
.venv/bin/ruff format .               # tidy the code layout
```

GitHub Actions runs the same checks, against PostgreSQL, on every push (`.github/workflows/ci.yml`).

The front-door compatibility suite talks to a real server over SSH, so it isn't part of `manage.py test`; see
[`tests/door_compat/README.md`](tests/door_compat/README.md).

## Settings

All settings come from environment variables (or `.env` locally); see `.env.example` and `bbms/settings.py`.
The important ones in production are `SECRET_KEY`, `DATABASE_URL` (PostgreSQL), `ALLOWED_HOSTS`,
`CSRF_TRUSTED_ORIGINS`, `SECURE_SSL_REDIRECT=True` and the email settings. **Never commit secrets.**

## Dependencies

Top-level packages are listed in `requirements.in` (and `requirements-dev.in` for tools). The exact versions are
pinned in `requirements.txt`. To add or upgrade a package, edit the `.in` file and run:

```bash
uv pip compile requirements.in -o requirements.txt
uv pip compile requirements-dev.in -o requirements-dev.txt
```

## Deployment

The app is a Docker image (`Dockerfile`) for DigitalOcean App Platform, with managed PostgreSQL and Spaces
(spec 01 §1.8). Database migrations run as a pre-deploy job (`python manage.py migrate`). Testing before the
switch-over from the legacy system is described in spec 08 §8.3.
