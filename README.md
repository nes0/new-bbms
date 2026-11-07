# BBMS: Build Brighton Member System (rebuild)

The membership system for Build Brighton: members, payments, door and machine access, and more. This is the
**new Django version**, replacing the legacy Laravel/PHP system. What it must do is described in
[`docs/spec/`](docs/spec/README.md); start there.

**Status:** early skeleton. Members can log in (including with passwords migrated from the legacy system) and
admins can manage members. Everything else is still to be built.

## What's in this repository

| Path | What it is |
|---|---|
| `bbms/` | Django project settings and top-level URLs |
| `apps/core/` | Site-wide pieces: home page, health check (`/healthz`), shared templates |
| `apps/members/` | Members and their login accounts, including the legacy password hasher |
| `templates/`, `static/` | Page templates; CSS and HTMX (kept locally, no build step) |
| `docs/spec/` | The specification |
| `tests/door_compat/` | Runs the real front-door firmware against a server (needs the `BBMSDoorEntry` clone) |
| `migration/` | Queries and scripts for moving data from the legacy system |

Apps still to come (one per area of the spec): payments and credit, subscriptions and billing, access control and
the device API, equipment and inductions, storage, governance (proposals, expenses), reports.

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
