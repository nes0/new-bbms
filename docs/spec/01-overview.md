# 1. Overview

## 1.1 Purpose

BBMS runs the membership of Build Brighton, a makerspace. It is the system of record for:

- **who is a member** and whether they are paid up,
- **collecting money** — monthly subscriptions (mostly GoCardless Direct Debit), deposits, induction fees, equipment usage,
- **who may open the door** and **who may use which machines** (via RFID key fobs and networked access-control devices),
- **governance** — member proposals and voting, expense reimbursement, delegated roles.

Door and equipment devices keep a local cache of allowed fobs, so a short BBMS outage doesn't lock keyholders out (05).
But nightly billing, new fobs and revocations all depend on BBMS, so it is still **critical infrastructure** for the space.

## 1.2 Users

| Who | What they do |
|---|---|
| **Prospective member** | Registers online, sets up a Direct Debit, completes the online induction (agrees rules). |
| **Member** | Manages own details/profile/photo, sees payments and charges, tops up credit, pays fees, pays for a box, rents lockers, votes, submits expenses, views equipment, the member directory and who is in the space. |
| **Fob holder** | Any active member: a free fob issued by a trustee. Taps in at the door to record visits (counts towards trusted status) and uses the machines they're trained on; doesn't open the door. |
| **Trusted member / keyholder** | A member who has been granted 24/7 door access (needs `trusted` + `key_holder` + approved photo + an RFID fob + a physical key, £15 key deposit). |
| **Trainer** | Trains other members on a piece of equipment. |
| **Role holders** | Members with delegated duties (see §1.3). |
| **Admin (trustees)** | Everything: edit any member, approve photos/addresses, grant trusted/keyholder, issue fobs, record cash, manage roles, approve expenses, create proposals, change settings. |
| **Devices** | Door controllers and equipment controllers calling the device API. |
| **Payment providers** | GoCardless (webhooks) and Stripe (card payments). |

## 1.3 Roles

Roles are named groups; a member can hold several. Each role has a title, description, optional public/private
email address and chat channel (shown on the "groups" page).

| Role key | Grants (from legacy code) |
|---|---|
| `admin` | Full access to everything. Also implicitly passes most other checks. |
| `finance` | Payments list/overview, subscription charge list, bank statement import, all expenses. |
| `equipment` | Create/edit/archive equipment, upload photos. |
| `storage` | Release any member's locker; follow up unpaid locker rent; set or clear members' box flag. |
| `comms` | Confirm members' general inductions. |
| `acs` | Manage access-control devices; receives "device offline" notifications. |
| `laser` (and any role named after an equipment slug) | Mark that equipment's usage sessions as training/testing (removes the charge); `laser` may email all laser-trained members. |
| `storage-box-user` | Special: account exists only to rent storage; **cannot vote**. |
| `trustees` | Referenced once in code. ⚠️ Verify whether still used. |

"**Member**" is not a stored role — it means "logged in and not banned".

## 1.4 Scope

### In scope
Everything in files 03–06.

**New features** (not in the legacy system): **locker rental** at a monthly rent, alongside storage boxes (now just a
"paid for a box" flag, one-off fee), with an admin-editable inventory, per-locker rent overrides, per-member locker limits and
reserved / out-of-use lockers (06 §6.2, added 2026-11-06).

**Moodle integration** (06 §6.12, added 2026-11-07): "Log in with BBMS" single sign-on for Moodle, equipment
linked to Moodle courses, and course completion required before sign-off and machine use.

### Dropped (decided 2026-11-05)
These legacy features will **not** be rebuilt:

| Legacy feature | Notes |
|---|---|
| Old fixed-amount GoCardless *subscriptions* (`payment_method = gocardless`) | Any remaining members are moved to variable Direct Debit (mandate + per-charge payments) before cutover. |
| PayPal subscriptions and IPN | Including `secondary_email` (only used to match PayPal payers) and "unknown PayPal donation" handling. |
| Legacy device endpoints `/access-control/*`, `/acs`, `/acs/spark` | The front door uses only the `/acs/*` API (05). A new device API is being designed alongside new firmware. |
| CCTV image upload → animated GIFs | |
| Detected devices (MAC address log) | |
| Pusher real-time activity page | The activity page stays (members can see who was in the space when). |
| Swagger API docs page | Replaced by generated OpenAPI docs for the device API. |
| Slack notifications | All calls were already disabled. A chat webhook can be added later if wanted. |
| Balance transfers between members | Already disabled in legacy. |
| Emergency key-safe PIN | Defunct. The setting, "reveal door key" button and PIN admin form are removed. |
| Hand-built log viewer | Replaced by hosted logging/monitoring (§1.8). |

## 1.5 Non-functional requirements

- **Maintainable by novices**: one language end-to-end, mainstream framework, minimal custom JavaScript, no front-end build step, clear tests for the money and access rules.
- **Reliable**: device checks answer in < 500 ms; scheduled jobs are idempotent (safe to re-run) and alert someone on failure.
- **Secure**: CSRF protection, role checks on every route, signed webhooks, an API key per device, no secrets in the repo, an audit log of admin actions.
- **Private**: member PII (address, phone, emergency contact, bank details) visible only to the member and admins; photos not publicly listable.
  Proofs of identity are seen only by those allowed to check them and are deleted once checked (03 §3.1).
- **Money is exact**: all money stored as **integer pence**. (Legacy mixes pounds-as-float and pence-as-int.)
- **Prices are settings, not code** — key deposit, box fee, locker rents per size, locker limit, minimum subscriptions, grace periods (see 02 §2.8).
- **Cheap to host**: one small managed app plus one database.
- **Backups**: daily database backup with a tested restore.

## 1.6 Technology (decided 2026-11-05)

**Django (Python) + server-rendered HTML with HTMX**, packaged as a Docker image.

- Python is the language makerspace volunteers are most likely to know.
- Django's **built-in admin** gives trustees a working back office (members, fobs, roles, equipment, payments, settings)
  with very little code. A large share of the legacy app is admin CRUD.
- Batteries included: auth, password reset, CSRF, forms, migrations, email, a test framework.
- Scheduled jobs are plain management commands (e.g. `python manage.py bill_members`), each idempotent.
- Device API: JSON views (Django REST Framework or plain views), API-key auth per device, OpenAPI docs.

**Database: PostgreSQL** (recommended; ❓ confirm). At DigitalOcean, managed PostgreSQL and managed MySQL cost the
same, so MySQL saves nothing. Django is also built and tested primarily against PostgreSQL, which matters here:
- **Migrations are transactional** on PostgreSQL: a failed schema change rolls back cleanly. On MySQL a failed
  migration can leave the database half-changed, which is hard for a novice to fix.
- Better support for **constraints** that protect the money and access rules (check constraints, partial unique
  indexes, e.g. "one open charge per member per month").
- First-class JSON fields and full-text search, and reliable row locking for the billing jobs.

## 1.7 Open decisions (for trustees)

1. Door access by an explicit list of membership statuses rather than the `active` flag (05 §5.2).
2. Proposal **quorum** rule, and whether only current members may vote (06 §6.3).
4. Minimum subscription: **self-service decided 2026-11-07: £25** (was £20), an admin setting (02 §2.8). Still open:
   keep the £5 minimum for amounts set by an admin?
5. Who may mark members as **trained**: admins only, or trainers too (05 §5.8)?
6. Should the door's public Discord announcement use the full name, the first name only, or respect private profiles (05)?
7. Public (logged-out) member directory: keep (06 §6.1)?
8. Storage (06 §6.2): default locker limit (suggested 1); first-month rent (suggested pro rata); rent changes for existing renters (suggested 30 days' notice); unpaid rent (suggested: locker may be cleared after 30 days); refunds when a locker is given up (suggested none); whether the box flag survives leaving and rejoining (suggested yes); whether the storage team manages the locker inventory.
9. Group email for every equipment team, not just laser (06 §6.6).
10. Policies as Markdown in the repo, or editable by admins (06 §6.8)?
11. Retention period for access and usage logs (08).
12. Database: confirm PostgreSQL (§1.6).
13. Fobs and keys (05 §5.1–5.2): ~~who may issue fobs~~ (decided: trustees, as a permission a role can be given); ~~free replacement for a lost fob~~ (decided: free); key deposit refunded when the key is returned (suggested yes, kept if lost).
14. Attendance threshold for the trusted-status review list (05 §5.10; suggested 8 visits over at least 4 weeks).
15. Moodle (06 §6.12): who may log in to Moodle (suggested any non-banned account); online training expiry (suggested none); members already signed off when a course is added (suggested they keep access).
16. Reports (04 §4.9, 06 §6.13): who sees membership statistics (suggested all members; money figures finance and admins only); whether to build expenditure tracking later.
17. Proof of identity (03 §3.1): whether an unchecked ID blocks anything (today it doesn't); who checks (suggested
    trustees); how long unchecked uploads are kept (suggested 30 days); whether members who emailed documents before
    need re-checking (suggested no, but the emails are deleted).

## 1.8 Deployment (decided 2026-11-05: managed platform on DigitalOcean)

The new system runs on a
**managed platform**, so the OS, TLS certificates and database patching are the provider's job, not a volunteer's.

| Piece | Choice | Notes |
|---|---|---|
| Web app | **DigitalOcean App Platform**, Docker image built from the repo | Deploys on push to `main` after the tests pass (GitHub Actions). Smallest paid instances start at about $5–12/month. |
| Scheduled jobs | App Platform **scheduled (cron) jobs** running management commands | Billed only while running. The minimum interval is reportedly **15 minutes**, so the "device offline" check runs every 15 minutes instead of every 10 (devices send a heartbeat every 15 minutes anyway). |
| Database | **DigitalOcean Managed PostgreSQL**, smallest node (1 GiB, about $15/month) | Includes automatic daily backups and point-in-time restore. Don't use App Platform's "dev database" ($7): it isn't backed up and is deleted with the app. |
| File storage | **DigitalOcean Spaces** (S3-compatible), private bucket | Member photos, equipment photos, expense receipts. Served through the app or short-lived signed URLs, never public-read. |
| Email | A transactional email service (e.g. Postmark, Mailgun, SES) | Welcome, payment, expense and password emails. |
| Domain & TLS | `bbms.buildbrighton.com` pointed at App Platform; certificate managed automatically | The current door firmware requires this exact hostname with a valid certificate. |
| Secrets | App Platform encrypted environment variables | Never in the repo or the Docker image. Includes the Moodle web-service token and the OIDC signing key. |
| Monitoring | Error tracking (e.g. Sentry free tier), an uptime check on the device API, and a heartbeat check per scheduled job (e.g. Healthchecks.io) | A missed 01:30 billing run must alert a named person. |
| Static files | Served by the app (WhiteNoise) | No separate web server needed. |

**Estimated running cost:** about **$25–35/month** (app ~$5–12, Postgres ~$15, Spaces ~$5, plus a few dollars for jobs),
excluding email. Prices are from DigitalOcean's pricing pages in November 2026; check them before committing.

**Portability:** because the app is a standard Docker image plus PostgreSQL plus S3-compatible storage, it can move
to another platform or a single VPS later without code changes.
