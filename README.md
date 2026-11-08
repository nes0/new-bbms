# BBMS: Build Brighton Member System (rebuild)

The membership system for Build Brighton: members, payments, door and machine access, and more. This is the
**new Django version**, replacing the legacy Laravel/PHP system. What it must do is described in
[`docs/spec/`](docs/spec/README.md); start there.

**Status:** early. Built so far:

- members can log in (including with passwords migrated from the legacy system) and admins can manage them;
- online **registration** (`/register`) and proof-of-identity uploads checked by trustees (`/id-checks/`);
- the **general induction**: members read the guidance and the rules and agree (`/account/induction/`); the comms
  role confirms it (`/general-inductions/`);
- **admin member controls**: an audit log of trusted, key holder, induction, photo and banned changes, the "you're
  now trusted" email, the "trusted, with no photo" filter, and approving addresses from the member's admin page;
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
- the **legacy import** (`manage.py import_legacy`): members, addresses, photos (`--photos-from`), payments,
  charges, the audit log, fobs, equipment and the logs, with an option to mark everyone as inducted
  (`--mark-inducted`); tested on the legacy clone's data inside the Docker image;
- **lockers**: the inventory, default rents per size and location, price overrides for one member's rental and for
  an empty locker, availability, per-member limits and rentals in the admin, all logged; and the members' page
  (`/lockers/`). Self-service renting waits for its own slice;
- **subscriptions and billing**: members set up a **Direct Debit** (GoCardless) or pay from credit
  (`/account/payment/`), change how they pay, pay a failed charge from credit, leave and rejoin. Monthly
  subscription and **locker rent charges**, billed together on the payment day; the GoCardless webhook
  (`/gocardless/webhook`); failed Direct Debits retried as the settings say; suspension and leaving; billing emails.
  The scheduled jobs are below. Without a GoCardless token, development uses a fake (`/dev/gocardless/payments/`
  plays GoCardless's part);
- **card payments** through Stripe Checkout, and **one-off Direct Debits**: the member's Pay page (`/pay/`) for
  induction fees, the key deposit, the box fee and unpaid charges (by card, Direct Debit or from credit), and
  top-ups by card or Direct Debit on the credit page. Without a Stripe key,
  development uses a fake checkout page (`/dev/stripe/…`);
- **PayPal**: a monthly PayPal subscription for members without a UK bank account, and credit top-ups by PayPal,
  recorded from PayPal's notifications (`/paypal-ipn`, each checked with PayPal). Without `PAYPAL_ACCOUNT_EMAIL`,
  development uses a fake PayPal page (`/dev/paypal/`), and `/dev/paypal/tools/` plays PayPal's later notifications;
- **profiles and the member directory**: members edit their profile and upload a photo (`/account/profile/`),
  admins approve photos (Members → Member photos), members browse `/members/` and `/groups/`. Photos are only
  served through BBMS, to people allowed to see them;
- **Discord**: members link their Discord account (`/account/discord/`) before they can set up their payment; BBMS
  adds them to the server, sets their nickname to their real name, posts a welcome message (text in Settings) and
  keeps their member / former-member role right. Without the `DISCORD_*` settings, development uses a fake
  Discord page (`/dev/discord/`).

## What's in this repository

| Path | What it is |
|---|---|
| `bbms/` | Django project settings and top-level URLs |
| `apps/core/` | Site-wide pieces: home page, health check (`/healthz`), shared templates |
| `apps/members/` | Members and their login accounts, including the legacy password hasher |
| `apps/access/` | Key fobs, devices (door and machine readers), door decisions, the device API |
| `apps/equipment/` | Equipment, inductions, Moodle course completions, machine usage sessions and fees |
| `apps/site_settings/` | Settings the trustees change in the admin (prices, limits, reader timings, the space's name), with a log of changes |
| `apps/payments/` | The ledger, member credit, machine fees charged from credit, cash and manual payments, withdrawals, card (Stripe) and PayPal payments |
| `apps/storage/` | Lockers: locations, sizes, default rents, overrides, availability, rentals, limits and their log |
| `apps/profiles/` | Member profiles and photos (upload, approval, who may see them), the member directory, the groups page |
| `apps/discord/` | Linking members' Discord accounts, their roles on the server, the welcome message |
| `apps/billing/` | Subscription and locker rent charges, GoCardless (Direct Debit) and its webhook, the nightly billing and membership jobs |
| `templates/`, `static/` | Page templates; CSS and HTMX (kept locally, no build step) |
| `docs/spec/` | The specification |
| `tests/door_compat/` | Runs the real front-door firmware against a server (needs the `BBMSDoorEntry` clone) |
| `migration/` | Queries and scripts for moving data from the legacy system |
| `apps/legacy/` | The legacy import: `manage.py import_legacy` (spec 08 §8.2) |
| `tools/clone_test/` | How to build and run the Docker image on the legacy clone droplet, with its data imported |

Still to come (one app per area of the spec): the standing-order statement import, the box flag and
self-service locker renting, governance (proposals, expenses), notifications, reports.

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

### Trying pages in a headless browser (optional)

[Playwright](https://playwright.dev/python/) (in `requirements-dev.txt`) drives a real Chromium without a screen,
for clicking through pages and taking screenshots. It isn't part of `manage.py test`. Set it up once:

```bash
.venv/bin/playwright install --only-shell chromium   # downloads Chromium to ~/.cache/ms-playwright (no root needed)
```

Chromium needs some system libraries. On Ubuntu 24.04 (needs `sudo`):

```bash
sudo apt-get install -y libatk1.0-0t64 libatk-bridge2.0-0t64 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2t64 libatspi2.0-0t64
```

To see what's still missing, run `ldd` on the downloaded `chrome-headless-shell` and look for "not found". On other
systems, `sudo .venv/bin/playwright install-deps chromium` installs everything it needs (a longer list, including
fonts). Then, with `manage.py runserver` running:

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto("http://127.0.0.1:8000/register")
    page.screenshot(path="register.png", full_page=True)
    browser.close()
```

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

The step-by-step guide to setting up production, with what each outside service needs, is
[docs/deployment.md](docs/deployment.md) (a first draft, with placeholders for what isn't built yet).

## Scheduled jobs

Plain management commands, run by the platform's scheduler (spec 03 §3.6), Europe/London time. Each is safe to run
twice; each takes `--date YYYY-MM-DD` (to catch up a missed night) and `--dry-run` (report only: nothing is changed,
sent to GoCardless or emailed).

| When | Command |
|---|---|
| 01:00 | `manage.py create_charges` |
| 01:30 | `manage.py bill_members` |
| 06:00 | `manage.py check_memberships` |
| 07:00 | `manage.py retry_direct_debits` |
| every 15 minutes | `manage.py process_gocardless_events` (a backstop: the webhook normally handles events at once) |
| daily | `manage.py shred_stale_id_documents` |
| nightly | `manage.py sync_discord` (Discord member / former-member roles; welcome messages that failed) |
| hourly | `manage.py tidy_equipment_sessions` |
