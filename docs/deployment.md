# Deploying BBMS to production

**Status: first draft (2026-11-08).** This is the step-by-step for putting BBMS on DigitalOcean's managed container
platform (App Platform), as decided in [spec 01 §1.8](spec/01-overview.md). It is written for a volunteer who has not
done this before. It lists everything you need from outside services, and every place where the code or the setup
isn't finished yet.

How to read it:

- 🚧 **Not built yet** marks something this guide needs that doesn't exist in the code or the repo. Each has a
  placeholder so the step is not forgotten.
- ⚠️ **Verify** marks a detail about DigitalOcean or another service that was written from memory and should be
  checked against their current documentation before you rely on it.
- ❓ marks a decision for the trustees.
- Replace anything in `<angle brackets>` with your own value. **Never put a real secret in this file or the repo.**

The cutover itself (switching the doors, the members and GoCardless from the old system) is planned in
[spec 08 §8.3](spec/08-migration-and-cutover.md). This guide covers building the new site; that section says when to
switch.

---

## 1. What you are building

| Piece | What it is | Where it lives |
|---|---|---|
| **Web app** | The Django site, the device API (`/acs/…`, `/api/devices/v2/…`) and the webhooks, run by gunicorn from the `Dockerfile` | App Platform *web service* |
| **Migration step** | `python manage.py migrate`, run before each new version starts | App Platform *pre-deploy job* |
| **Scheduled jobs** | Billing, membership checks, device checks and so on: plain management commands | App Platform *scheduled jobs* |
| **Database** | PostgreSQL | DigitalOcean *Managed Database* |
| **Files** | Member photos (private) | DigitalOcean *Spaces* bucket |
| **Email** | Welcome, payment and password emails | A transactional email service (§6.5) |
| **Payments** | Direct Debit, cards, PayPal | GoCardless, Stripe, PayPal |
| **Discord** | New members join the server from BBMS | Discord application and bot |

Everything the app needs is passed in as **environment variables**. The list is in §5, and the same names are in
`.env.example`.

Estimated cost is about $25–35 a month plus email (spec 01 §1.8). ⚠️ Check DigitalOcean's current prices.

---

## 2. What you need before you start

Tick these off first; most take days, not minutes.

| Need | Who | Notes |
|---|---|---|
| A DigitalOcean account with a payment method | A trustee | Create a *Project* called `bbms` to keep everything together. |
| The GitHub repository, with admin rights | A trustee | App Platform needs permission to read it. |
| Control of the DNS for `buildbrighton.com` | Whoever runs the domain | You'll add one record for `bbms.buildbrighton.com` (§4). |
| A GoCardless **live** account, approved | Treasurer | Approval can take days. Do the sandbox first (§6.1). |
| A Stripe account, activated for live payments | Treasurer | Needs the space's bank details. |
| A PayPal Business account (the new one decided in 04 §4.4) | Treasurer | The old account stays only for members who haven't moved. |
| An email sending service, with the domain verified | Whoever runs the domain | SPF and DKIM records are added to DNS. |
| A Discord server, and an application with a bot in it | A Discord admin | §6.4. |
| A password manager shared by the trustees | Trustees | Every secret below is stored here **first**, then pasted into DigitalOcean. |
| The legacy database (read-only user) | Whoever has the old server | Only needed for the import (§8). |

❓ **Who is the named owner of each account?** Write it in the table at the end of this file. When the person leaves,
their accounts must have a second owner.

---

## 3. Create the DigitalOcean pieces

Do all of these in the same region, so they are close to each other. The spec's examples use **London (`lon1`)**.

### 3.1 The database

1. *Create → Databases → PostgreSQL*, the current major version, the smallest node (⚠️ about $15 a month), in `lon1`.
2. Put it in the `bbms` project. Name it `bbms-db`.
3. **Do not use App Platform's built-in "dev database".** It isn't backed up and is deleted with the app.
4. In *Settings → Trusted sources*, allow only the app (add it after §3.3). Anyone else, including your laptop, is
   refused unless you add their address.
5. Create a database called `bbms` and a user called `bbms` (the default `doadmin` user is for emergencies only).
6. Copy the **connection string** for that user. It looks like
   `postgres://bbms:<password>@<host>:25060/bbms?sslmode=require`. This becomes `DATABASE_URL` (§5). Store it in the
   password manager.
7. Confirm *Backups* shows daily backups and point-in-time recovery. **Test a restore before cutover** (§10).

### 3.2 The photos bucket (Spaces)

1. *Create → Spaces*, in `lon1`. Name it for the site, e.g. `bbms-prod-files`. Spaces names are shared across all of
   DigitalOcean, so you may need to add a suffix.
2. **Leave "File listing" restricted (private).** Photos are served by BBMS through short-lived signed links, never
   public (spec 02 §2.9, 01 §1.8).
3. *API → Spaces Keys → Generate*. Copy the key and secret straight into the password manager. They become
   `SPACES_KEY` and `SPACES_SECRET`.
4. Note the bucket name (`SPACES_BUCKET`), and the endpoint (`SPACES_ENDPOINT`, e.g.
   `https://lon1.digitaloceanspaces.com`) and region (`SPACES_REGION`, `lon1`).
5. 🚧 **Not built yet:** equipment photos and expense receipts also go here (spec 02 §2.9) once those features exist.
   The same bucket and keys will be used, so nothing more to set up.

Spaces is **not** backed up by DigitalOcean the way the database is. ❓ Decide whether photos matter enough to copy
elsewhere. (The import can fetch them again from the old bucket while it still exists; §8.)

### 3.3 The app

You can click through the web console, or keep the settings in a file (`.do/app.yaml`) and upload it with `doctl`,
DigitalOcean's command line tool. A file is better because it is reviewed like code.

🚧 **Not built yet:** there is no `.do/app.yaml` in the repo. The sketch below shows what it should contain.
⚠️ **Verify** every field name against DigitalOcean's current *App Spec reference* before using it.

```yaml
name: bbms
region: lon
services:
  - name: web
    dockerfile_path: Dockerfile
    github:
      repo: <github-org>/<repo>
      branch: main
      deploy_on_push: true
    http_port: 8080
    instance_size_slug: <smallest paid size>
    instance_count: 1
    health_check:
      http_path: /healthz
    envs:
      - { key: SECRET_KEY, scope: RUN_TIME, type: SECRET, value: "<from password manager>" }
      # … every variable in §5 …
jobs:
  - name: migrate
    kind: PRE_DEPLOY
    dockerfile_path: Dockerfile
    run_command: python manage.py migrate --noinput
    # same envs as web
  - name: create-charges
    kind: SCHEDULED   # the schedule is set in the job's cron field, see §7
    run_command: python manage.py create_charges
    # … one entry per job in §7 …
databases:
  - name: bbms-db
    engine: PG
    production: true
    cluster_name: bbms-db      # the managed database from §3.1
```

Points to know:

- **The Dockerfile already does the build work**: it installs the requirements, collects the static files and
  runs gunicorn on `$PORT` (8080). It deliberately does **not** run migrations; the pre-deploy job does, once per
  release, so two web instances never migrate at once.
- **Web workers:** `WEB_CONCURRENCY` (default 2) sets how many gunicorn workers run. Two is plenty for a makerspace;
  the door and machines make a request every few minutes.
- **One web instance is enough to start.** BBMS keeps no state in memory, so you can add more later.
- **Health check:** `/healthz` answers 200 only if the app can reach the database. It's also the address for the
  uptime monitor (§6.6).
- ⚠️ **Verify the health check with HTTPS redirect on.** `SECURE_SSL_REDIRECT=True` (§5) makes Django redirect plain
  HTTP to HTTPS. If DigitalOcean's health check arrives over plain HTTP without the `X-Forwarded-Proto` header, it may
  be redirected and fail. 🚧 If so, the fix is a one-line exemption for `healthz` in `bbms/settings.py`
  (`SECURE_REDIRECT_EXEMPT`); it isn't there yet. DigitalOcean's own front end already redirects HTTP to HTTPS, so
  the setting could also be left off.
- **Deploys:** the spec says "deploys on push to `main` after the tests pass". `.github/workflows/ci.yml` runs lint,
  `check --deploy`, the migration check and the tests, but does not deploy. ⚠️ Verify that App Platform can wait for
  those checks (a "wait for CI" option on auto-deploy) so a failing build is never deployed. 🚧 Until confirmed,
  deploy by hand from a commit whose CI run is green.

---

## 4. Domain and HTTPS

1. In the app's *Settings → Domains*, add `bbms.buildbrighton.com`.
2. DigitalOcean shows a **CNAME** target. Add that record in the domain's DNS. Certificates are issued and renewed
   automatically.
3. Set `SITE_URL=https://bbms.buildbrighton.com`, `ALLOWED_HOSTS=bbms.buildbrighton.com` and
   `CSRF_TRUSTED_ORIGINS=https://bbms.buildbrighton.com`.

The current door firmware needs **this exact hostname with a valid certificate** (spec 01 §1.8). Don't change the
name later without checking the doors.

While rehearsing, deploy to the app's own `*.ondigitalocean.app` address and use that for `ALLOWED_HOSTS`, then switch
the domain over at cutover (spec 08 §8.3, phase 4).

`SECURE_HSTS_SECONDS` starts at 3600 (1 hour). Once everything works, raise it in steps (a day, a month, a year).
Browsers remember it, so a mistake with a long value is hard to undo.

---

## 5. Environment variables

Set these on the **web service and on every job** (jobs run the same code and need the same database and keys). In
the console, mark the secrets as *encrypted*.

### 5.1 Required

| Variable | Value | Notes |
|---|---|---|
| `DEBUG` | `False` | Never `True` in production. |
| `SECRET_KEY` | A long random string | `python -c "import secrets; print(secrets.token_urlsafe(64))"`. The site won't start without one. Changing it logs everyone out. |
| `DATABASE_URL` | From §3.1 | Includes `?sslmode=require`. |
| `ALLOWED_HOSTS` | `bbms.buildbrighton.com` | Comma-separated. |
| `CSRF_TRUSTED_ORIGINS` | `https://bbms.buildbrighton.com` | Comma-separated. |
| `SITE_URL` | `https://bbms.buildbrighton.com` | Used in emails and QR codes. |
| `SECURE_SSL_REDIRECT` | `True` | See the health check note in §3.3. |
| `SECURE_HSTS_SECONDS` | `3600`, later more | §4. |
| `ID_DOCUMENT_KEYS` | A Fernet key | Encrypts proofs of identity. Make one with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. **Keep a copy only in the password manager.** Never in the repo, the database or any shadow or staging site. The site refuses ID uploads without it. To change it later, see §10. |

### 5.2 Photos (Spaces)

| Variable | Value |
|---|---|
| `SPACES_BUCKET` | The bucket name (§3.2). Without it, photos go to the container's disk and are **lost at every deploy**; `check --deploy` warns (`members.W002`). |
| `SPACES_ENDPOINT` | `https://lon1.digitaloceanspaces.com` (default) |
| `SPACES_REGION` | `lon1` (default) |
| `SPACES_KEY`, `SPACES_SECRET` | From §3.2 |

### 5.3 Payments and Discord

These are filled in §6. Leave each out until its account is ready: the site then switches that feature off cleanly
and `check --deploy` lists what is missing.

| Variable | From |
|---|---|
| `GOCARDLESS_ACCESS_TOKEN`, `GOCARDLESS_ENVIRONMENT` (`sandbox` or `live`), `GOCARDLESS_WEBHOOK_SECRET` | §6.1 |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | §6.2 |
| `PAYPAL_ACCOUNT_EMAIL`, `PAYPAL_SANDBOX` (`False` on the live site), `PAYPAL_OLD_ACCOUNT_EMAILS` | §6.3 |
| `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_MEMBER_ROLE_ID`, `DISCORD_FORMER_MEMBER_ROLE_ID`, `DISCORD_WELCOME_CHANNEL_ID` | §6.4 |
| `MOODLE_TOKEN` (web-service token), `OIDC_RSA_PRIVATE_KEY` (signs "Log in with BBMS"; a secret). Moodle's address is an admin setting, not a variable. | §6.7 |

### 5.4 Email and other

| Variable | Value |
|---|---|
| `DEFAULT_FROM_EMAIL` | e.g. `BBMS <noreply@bbms.buildbrighton.com>` (the default) |
| `EMAIL_HOST` | Your provider's SMTP server, e.g. `smtp.example.com`. **Setting this turns email on**; without it emails are only printed to the log (§6.5). |
| `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | The SMTP login (the password is a secret: mark it encrypted). |
| `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_USE_SSL` | Defaults: port `587` with STARTTLS. For a provider that wants port 465, set `EMAIL_PORT=465` and `EMAIL_USE_SSL=True` (the two can't both be on). |
| `EMAIL_TIMEOUT` | Seconds to wait for the mail server (default 10). |
| `EMAIL_BACKEND` | Normally left out: SMTP is used when `EMAIL_HOST` is set. Set it only to use a provider's own Django package. |
| `DB_CONN_MAX_AGE` | Seconds to keep database connections open (default 60). |
| `LOG_LEVEL` | `INFO` (default). Logs go to the console, which App Platform collects. |
| `WEB_CONCURRENCY` | gunicorn workers (default 2). |
| `LEGACY_DATABASE_URL` | Only for the import (§8). Don't leave it set afterwards. |
| `HEARTBEAT_CREATE_CHARGES`, `HEARTBEAT_BILL_MEMBERS`, `HEARTBEAT_CHECK_MEMBERSHIPS`, `HEARTBEAT_RETRY_DIRECT_DEBITS` | Each nightly billing job's ping URL (§6.6), e.g. `https://hc-ping.com/<uuid>`. Treat them as secrets: anyone with one can fake that job's ping. `check --deploy` names any that are missing (`billing.W004`). |

🚧 **Not built yet:** variables for error tracking and for heartbeats on the other jobs (§6.6), and for the bank statement import (04 §4.6). They will be
added here when those features exist.

---

## 6. What each outside service needs

Do the **sandbox / test** version of each first, on a staging copy (§9), then repeat for live.

### 6.1 GoCardless (Direct Debit)

1. Create the account; complete verification so live payments are enabled. ⚠️ Verify what GoCardless asks for.
2. *Developers → Create → Access token*. Use **Read/write** for the live site. Copy it into `GOCARDLESS_ACCESS_TOKEN`
   and set `GOCARDLESS_ENVIRONMENT` to `live` (or `sandbox` while testing; sandbox is a separate account).
3. *Developers → Webhook endpoints → Create*:
   - **URL:** `https://bbms.buildbrighton.com/gocardless/webhook`
   - **Secret:** make one up, store it, and put the same value in `GOCARDLESS_WEBHOOK_SECRET`.
   - The app uses `payments`, `mandates` and `billing_requests` events. Send all of them, or at least those.
4. Direct Debit set-up uses GoCardless's *billing request flow* (hosted pages). Make sure the hosted pages are enabled
   and branded for Build Brighton.
5. ❓ **Retry mode:** "BBMS retries" (the default) or "let GoCardless retry" (needs their Success+ plan). It's a
   setting in the site admin (03 §3.6, 02 §2.8).
6. Check: `python manage.py check --deploy` no longer shows `billing.W001` or `billing.W002`. Then run a sandbox
   payment end to end: set-up, charge, confirmed, failed, mandate cancelled (08 §8.4).

Members who pay by an old-style GoCardless set-up have **no payment method** after the import and must move to the
new Direct Debit before cutover (spec 08 §8.2).

### 6.2 Stripe (cards)

1. Activate the account for live payments.
2. *Developers → API keys*: copy the **secret key** into `STRIPE_SECRET_KEY`. Use `sk_test_…` until the site is live.
   Card details never reach BBMS: members pay on Stripe's hosted Checkout page.
3. *Developers → Webhooks → Add endpoint*:
   - **URL:** `https://bbms.buildbrighton.com/stripe/webhook`
   - **Events:** `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
     `checkout.session.async_payment_failed`, `checkout.session.expired`.
   - Copy the **signing secret** (`whsec_…`) into `STRIPE_WEBHOOK_SECRET`.
4. Check: no `payments.W001` or `payments.W002` from `check --deploy`.
5. Refunds are done in Stripe's dashboard and recorded by an admin (04 §4.3).

### 6.3 PayPal

1. A **new** Business account is used at cutover (spec 04 §4.4). Turn on **IPN** (instant payment notification) in
   its settings, so PayPal sends payment notices. Each button BBMS makes sets its own notification address, so no
   fixed IPN URL has to be entered, but make sure IPN is switched on. ⚠️ Verify the setting's current location.
2. Set `PAYPAL_ACCOUNT_EMAIL` to the account's email, and `PAYPAL_SANDBOX=False` on the live site only.
3. While members move across, list the old account's email in `PAYPAL_OLD_ACCOUNT_EMAILS` so their payments are still
   recorded (but nobody is asked to pay it).
4. The notices arrive at `https://bbms.buildbrighton.com/paypal-ipn`. They're checked back with PayPal before
   anything is recorded. Failed ones are kept in the admin (*Payments → PayPal notifications*) for a retry.
5. Check: no `payments.W003` from `check --deploy`.

### 6.4 Discord

1. In the Discord developer portal, create an **application**. Under *OAuth2*, add the redirect
   `https://bbms.buildbrighton.com/account/discord/done/`. Copy the **client ID** and **client secret**.
2. Under *Bot*, create the bot and copy its **token** (`DISCORD_BOT_TOKEN`). Invite the bot to the server with
   the permissions **Create Invite**, **Manage Roles** and **Manage Nicknames**, plus **Send Messages** in the
   welcome and announcement channels (the list is in `apps/discord/client.py`).
3. In the server's *Roles*, drag the bot's role **above** the *member* and *former member* roles it manages.
4. Turn on Developer Mode, then right-click to copy the IDs: the server (`DISCORD_GUILD_ID`), the member role
   (`DISCORD_MEMBER_ROLE_ID`), the former-member role (`DISCORD_FORMER_MEMBER_ROLE_ID`), the welcome channel
   (`DISCORD_WELCOME_CHANNEL_ID`) and the channel where new proposals and their results are announced
   (`DISCORD_ANNOUNCEMENT_CHANNEL_ID`).
5. The welcome message's text is in the site admin (*Settings → Discord*).
6. Check: no `discord.W001`. The nightly `sync_discord` job (§7) keeps roles right.

### 6.5 Email

BBMS sends plain-text emails through Django's mail system: welcome, payment problems, ID checks, password resets and
others (spec 06 §6.6). In production you need a real sending service (Postmark, Mailgun, Amazon SES, or similar).

1. Create the account and **verify the sending domain** (add the SPF and DKIM records the service gives you to the
   domain's DNS). Without this, mail goes to spam.
2. Create an SMTP username and password (or an API key).
3. Set `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` and `DEFAULT_FROM_EMAIL` (§5.4). The sender address
   must be on the domain you verified in step 1, or the provider will refuse it. Without `EMAIL_HOST`, BBMS only
   prints emails to its log, and `check --deploy` warns (`core.W001`).
4. Send a test from a shell on the app (§6.8): `python manage.py sendtestemail you@example.com`. Also try a password
   reset from the login page, and check the message isn't in the spam folder.
5. **If the mail server is down,** BBMS carries on: a payment, a status change or a nightly job is never undone because
   an email couldn't be sent. The failure is logged (the kind of error and which email, never the address or text), and
   that email is **not** retried. Watch the logs after a provider outage, and use the admin to find who missed out.
   (A password reset asked for during an outage shows the member an error instead.)
6. Staging should send to a test mailbox, never to real members (spec 08 §8.3).

### 6.6 Monitoring (🚧 partly built)

The spec asks for three things (spec 01 §1.8). Only the billing jobs' heartbeats are built so far:

| Need | Plan | Status |
|---|---|---|
| **Uptime check** on the app and the device API | Any uptime service, checking `https://bbms.buildbrighton.com/healthz` every minute or so, and alerting a named person | 🚧 Choose a service and add the monitor. `/healthz` exists. |
| **Error tracking** | e.g. Sentry's free tier | 🚧 No package or setting yet: add `sentry-sdk`, a `SENTRY_DSN` variable and the few lines to start it. |
| **A heartbeat per scheduled job** | e.g. Healthchecks.io: each job pings a URL when it succeeds, and a missed ping alerts someone | **Built for the four nightly billing jobs** (2026-11-08); 🚧 not yet for the others in §7. |

**Setting up the billing heartbeats** (Healthchecks.io; any service with the same `/start` and `/fail` URLs works):

1. Make one check per job: `create_charges`, `bill_members`, `check_memberships`, `retry_direct_debits`.
2. Give each a **cron schedule** matching its job's schedule in §7 (same time zone), and a **grace time** of about
   an hour: enough for a slow night, short enough that the alert arrives before members notice.
3. Send the alerts to a **named person** (the treasurer, or whoever owns billing in §12), by email and ideally a
   second way, such as a phone notification.
4. Put each check's ping URL in its variable (§5.4) on **that job** (the web service doesn't need them).
5. Run each job once on the live app and see "Started" then "Up" on its check.

What a job sends: `<url>/start` as it begins, `<url>` with its summary line ("subscription 4, locker-rent 1") when it
finishes, or `<url>/fail` with the kind of error if it crashes. Never a member's name or the error's text, which could
include one. So an alert means one of: the job **didn't run** (no ping), it **failed** (fail ping), or it **started
but never finished** (start, then nothing).

A ping that can't be sent is logged as a warning and never stops or fails the job. Runs by hand with `--date` or
`--dry-run` don't ping: after catching up a missed night by hand, the check stays "down" until the next scheduled run
pings it, or until someone pauses or resumes it on Healthchecks.io.

Until uptime and error alerts exist, and for the jobs without a heartbeat, someone must check the job logs each
morning.

### 6.7 Moodle (online training)

Two separate links to Moodle (spec 06 §6.12). **"Log in with BBMS"**: Moodle sends members to BBMS to log in, so
passwords live only in BBMS. **Course completion**: BBMS asks Moodle whether a member has finished a course, and
stores the answer; machines use only the stored answer, so a Moodle outage never blocks a machine.

**Rehearse everything below on a copy of Moodle first.** You switch the live Moodle's login at cutover (spec 08 §8.3,
phase 4), and a mistake can lock members out of Moodle.

**A. The signing key** (for "Log in with BBMS")

1. Make a key: `openssl genrsa -out bbms-oidc.pem 3072`. Store the file in the password manager, then delete it from
   your computer.
2. Set `OIDC_RSA_PRIVATE_KEY` to the file's contents, with each line break written as `\n` (one line). A way to
   print it that way: `awk 'NF {sub(/\r/, ""); printf "%s\\n",$0;}' bbms-oidc.pem`. Mark the variable encrypted.
3. Without the key, `/o/authorize/` returns "not found" and `check --deploy` warns (`moodle.W001`). Changing the key
   later only signs Moodle's members out of Moodle; they log in again.

**B. A web-service account in Moodle** (for course completion)

1. In Moodle: *Site administration → Server → Web services → Overview*. Enable web services and the **REST**
   protocol.
2. Create a dedicated user, e.g. `bbms-webservice`, with a role that has only what's needed. ⚠️ Verify the exact
   capabilities on your Moodle version: it must be able to look up users by ID number and email, and to view course
   completion for any user in the linked courses.
3. Create an *external service* (e.g. "BBMS") with exactly two functions: `core_user_get_users_by_field` and
   `core_completion_get_course_completion_status`. Authorise the user for it, and create a **token** for that user.
4. Put the token in `MOODLE_TOKEN` (encrypted). Without it BBMS can't check courses (`moodle.W002`).
5. Each linked course must have **course completion** turned on, with its criteria set (in the course's settings).
   Otherwise Moodle never reports it as finished.

**C. Tell BBMS where Moodle is, and register Moodle as a client**

1. In the site admin: *Settings → Moodle's address*, e.g. `https://moodle.example.com` (no slash at the end).
2. From a shell on the app (§6.8): `python manage.py setup_moodle_sso`. It prints the endpoints and a **client ID and
   secret**. The secret is shown **once** (BBMS keeps only a hash). Copy it straight into Moodle. Run the command again
   if Moodle's address changes; `--new-secret` makes a new secret.

**D. Moodle's OAuth 2 service** (*Site administration → Server → OAuth 2 services → Custom*)

1. Client ID and secret from step C. Scopes: `openid profile email`. Endpoints as printed (authorization, token,
   userinfo), or the discovery address `https://bbms.buildbrighton.com/o/.well-known/openid-configuration`.
2. Field mappings (external → Moodle): `email` → email, `given_name` → first name, `family_name` → surname, and
   `sub` → **ID number**. BBMS finds a member in Moodle by that ID number, then by email.
3. Tick "show on login page" and name it "BBMS". ⚠️ Verify the option names on your Moodle version.
4. Turn off Moodle's own sign-up and password login once it works, keeping a **local fallback login** for Moodle's
   admins.

**What BBMS tells Moodle, and who may log in.** Any BBMS login that isn't banned and has a **confirmed email
address** may log in to Moodle, including members still setting up (so they can train before joining). Moodle receives
`sub` (the member's BBMS id), `email`, `given_name`, `family_name`, `name`, and `email_verified` (always true, because
unconfirmed members are stopped first and sent to confirm). That is what makes it safe for Moodle to match a login to
an existing Moodle account by email: no one can join BBMS with someone else's address and reach their Moodle account.
Still switch on Moodle's own "require email verification" on the OAuth 2 service if it has one (⚠️ verify the name),
as a second check. Existing Moodle accounts (made by the old database login) must be linked, not duplicated: check
one in the rehearsal. The import marks every member's email as confirmed (assumed checked by hand in the past), so
nobody is emailed at cutover and existing members aren't stopped from using Moodle; an admin can withdraw it for an
address that proves wrong (member's page, "Email address confirmed").

**E. Linking machines to courses.** In the admin, *Equipment*: set **Moodle course id** (the number in the course's
address: `…/course/view.php?id=7`). Machines that share an induction category normally share a course. Members already
signed off before then keep their access unless "course required for existing" is ticked (❓ spec 06 §6.12).

**F. Checking it works.** Log in to Moodle with BBMS as a test member; finish a test course; open
`/account/training/` and press "check now". Run `python manage.py check_moodle_completions` by hand. Check
`check --deploy` shows no `moodle.W` warnings. The 15-minute job is in §7.

**At cutover** switch the live Moodle's login to "Log in with BBMS", then remove Moodle's access to the old database:
its MySQL account and the firewall rule.

### 6.8 Reaching a shell on the app

Some steps (creating the first admin, checks, the import) need to run a command inside the app. ⚠️ Verify the current
way: App Platform has a **Console** tab for a running component, and `doctl apps` has a console command. Treat that
shell as having full access to real data.

---

## 7. Scheduled jobs

Each job is a management command and is **safe to run twice** (it won't double-charge). Add each as a *scheduled
job* using the same image and the same environment variables as the web service.

⚠️ **Verify the time zone.** App Platform schedules are probably in **UTC**, while the app works in Europe/London.
The times below are the spec's local times (03 §3.6). Choose UTC times that keep the same **order** and always fall
after local midnight, and write the chosen times in the table. ❓ The order matters more than the exact hour:
`create_charges`, then `bill_members`, with `check_memberships` and `retry_direct_debits` after.

| When (London) | Command | What it does |
|---|---|---|
| 01:00 daily | `python manage.py create_charges` | Creates subscription and locker-rent charges up to a week ahead |
| 01:30 daily | `python manage.py bill_members` | Bills them: from credit, or one Direct Debit per payment day |
| 06:00 daily | `python manage.py check_memberships` | Suspends members who haven't paid; marks members as left |
| 07:00 daily | `python manage.py retry_direct_debits` | Asks GoCardless to retry failed Direct Debits when the retry day comes |
| every 15 min | `python manage.py process_gocardless_events` | Backstop for webhook events stored but not handled |
| every 15 min | `python manage.py check_devices` | Tells the `acs` team when a monitored device has gone quiet |
| every 15 min | `python manage.py check_moodle_completions` | Asks Moodle whether members waiting for sign-off have finished their online course (does nothing until Moodle is set up) |
| hourly | `python manage.py tidy_equipment_sessions` | Closes stale machine sessions, charges finished ones, sends service reminders |
| hourly | `python manage.py update_proposals` | Opens proposals (emails voters, posts on Discord), counts closed ones, posts results |
| daily | `python manage.py shred_stale_id_documents` | Deletes proofs of identity nobody checked in time |
| nightly | `python manage.py sync_discord` | Puts members' Discord roles right |

Notes:

- The minimum interval for a scheduled job may be **15 minutes** (⚠️ verify). That's why the device check runs every
  15 minutes, not 10 as in legacy.
- The four billing jobs ping a heartbeat on every scheduled run (§6.6).
- `create_charges`, `bill_members`, `check_memberships` and `retry_direct_debits` take `--date YYYY-MM-DD` (run as if
  it were that day, to catch up a missed night) and `--dry-run` (work it out, report, change nothing, send nothing).
  After a missed run, run the missed one by hand from a shell, with `--dry-run` first.
- 🚧 **Not built yet:** the hourly proposals tally (spec 03 §3.6, `calculate-proposal-votes`) arrives with proposals.
  Placeholder: `<calculate_proposal_votes>`.
- 🚧 **Not built yet:** the nightly copy for the shadow site and its comparison report (spec 08 §8.3). Don't add them
  yet.
- **Turn off the old system's nightly jobs at cutover**, so nobody is billed twice (spec 08 §8.3, phase 4).

---

## 8. First deploy, step by step

1. **Build and test locally.** `ruff check .`, `ruff format --check .`, `python manage.py check --deploy`, and
   `python manage.py test` all pass. The CI workflow does the same.
2. **Create the pieces:** §3.1 (database), §3.2 (Spaces), then §3.3 (the app), with the §5.1 and §5.2 variables.
3. **Deploy.** The pre-deploy job runs `migrate` first. Watch the build log. When the app is healthy, open the
   `*.ondigitalocean.app` address.
4. **Run the deploy checks** from a shell on the app (§6.8):

   ```
   python manage.py check --deploy
   ```

   Read every warning. Expected until the services are set up: `billing.W001/W002`, `payments.W001/W002/W003`,
   `discord.W001`. Anything else needs fixing first. `members.E001` means a key in `ID_DOCUMENT_KEYS` isn't a valid key; `members.W001` means none is set.
5. **Create the first admin:** `python manage.py createsuperuser` (email and a strong password). Log in at `/admin/`.
6. **Set the site up in the admin:**
   - *Settings*: the space name, prices (key deposit, box fee, minimum subscription), the grace periods, the Direct
     Debit retry mode and the Discord welcome message (spec 02 §2.8). Check each against the trustees' decisions.
   - *Users → Groups*: the roles the code looks for by name: `admin`, `trustees`, `finance`, `storage`, `acs`,
     `comms`, and one for each machine's team (named after the machine). If you import from legacy, the roles are
     created for you (spec 08 §8.2). Fill in each role's title and description for the Groups page.
   - *Equipment* and *Devices*, if not imported.
7. **Connect the services** one at a time (§6), with test keys first. After each, run `check --deploy` and a test
   payment or message.
8. **Add the scheduled jobs** (§7), run each once by hand from a shell with `--dry-run`, and look at the logs.
9. **Set up monitoring** (§6.6) so a failing job or a down site tells a person.
10. **Rehearse the data import** on staging (§9), not production, until the report is clean.

### The legacy data import

The import fills an **empty** database from the old MySQL (spec 08 §8.2). It refuses to run if members already exist.

```
LEGACY_DATABASE_URL=mysql://<readonly-user>:<password>@<host>:3306/<database> \
  python manage.py import_legacy --dry-run
```

`--dry-run` imports, prints a report, then undoes everything. Read the report; it shows only legacy ids and counts,
so it can be shared. Then run it again without `--dry-run`. Options for cutover:

- `--mark-inducted`: give every imported member a confirmed general induction. ❓ **Trustees to decide.** Without it,
  key holders whose induction legacy never confirmed lose 24-hour access (spec 05 §5.2).
- `--photos-from https://s3-eu-west-1.amazonaws.com/<legacy-bucket>/production`: copy photos from the old public
  bucket into the private Spaces bucket. Needs internet access, and `SPACES_BUCKET` set. 🚧 **Placeholder:** the
  legacy bucket's name.

🚧 **Decide how the import reaches the old database.** The legacy MySQL is bound to its own server. Options: run the
import from a trusted machine with `DATABASE_URL` pointing at the managed database (add that machine as a trusted
source, then remove it); or open the old server's MySQL to the app for the one night with a read-only user. Don't
expose MySQL to the internet. The import must run in a window where the old site is in maintenance mode (spec 08
§8.3, phase 4), because it copies a single moment in time.

After the import: members paying by old-style GoCardless must move to Direct Debit, and the report's "credit
differs" list must be explained (spec 08 §8.2, §8.4).

---

## 9. A staging copy (🚧 not set up yet)

Spec 08 §8.3 calls for a second app with its own database, using **sandbox/test** keys, a test mailbox and anonymised
data, so volunteers can try everything with fake money. Create it the same way as §3, with:

| Setting | Staging value |
|---|---|
| App name, database | `bbms-staging`, `bbms-staging-db` (never share the production database) |
| `GOCARDLESS_ENVIRONMENT`, token | `sandbox`, a sandbox token (a separate GoCardless sandbox account) |
| `STRIPE_SECRET_KEY` | `sk_test_…` |
| `PAYPAL_SANDBOX` | `True`, with a PayPal sandbox account |
| Email | A test mailbox, never real members |
| `ID_DOCUMENT_KEYS` | A **different** key from production |
| Data | Anonymised, **never** including `IdDocument` rows (spec 08 §8.3) |

The **shadow site** (a nightly copy of live data, run in dry-run mode, with read-only payment keys) is a further layer
(spec 08 §8.3, layers 3 and 4). 🚧 Not built yet; add its own section here when it is.

---

## 10. Running it afterwards

- **Updating:** merge to `main`. The pre-deploy job migrates, then the new version starts. To go back, redeploy the
  previous commit; a migration that has already run isn't undone, so keep migrations backwards-compatible for one
  release.
- **Backups:** the managed database keeps daily backups and point-in-time recovery. **Test a restore** into a new
  database before cutover and then every few months, and write down how long it took (spec 08 §8.4).
- **Rotating the ID encryption key:** put the new key first in `ID_DOCUMENT_KEYS` (comma-separated, old keys after
  it), deploy, run `python manage.py rotate_id_document_key`, then remove the old key.
- **Rotating other secrets:** change them in the provider first, then in DigitalOcean, then redeploy. Rotate any
  secret that has been seen by someone who has left.
- **Logs:** each component's logs are in the DigitalOcean console. Set `LOG_LEVEL=DEBUG` briefly when investigating.
  The code is written to log member ids rather than names or card details, but don't paste logs into public places.
- **Scaling:** raise the instance size or `WEB_CONCURRENCY` first; only add instances if the site is slow.
- **If the site is down:** devices on the v2 API fall back to their cached lists, but devices on the v1 API can't
  decide (spec 05 §5.4 explains each). Check the app's status, the database's status, then the recent deploys.
  🚧 Write a short "site is down" note for the doors here once the firmware behaviour is confirmed.

---

## 11. Before cutover: the checklist

Copy this into an issue and tick it off. The acceptance checks in
[spec 08 §8.4](spec/08-migration-and-cutover.md) must also pass.

- [ ] §3: database, Spaces bucket and app created, in `lon1`, in one project
- [ ] §4: domain points at the app and the certificate is valid; HSTS raised
- [ ] §5: every variable set, secrets stored in the password manager
- [ ] `check --deploy` shows no warnings
- [ ] §6.1–6.4: GoCardless, Stripe, PayPal and Discord live; one real small payment of each kind tested and refunded
- [ ] §6.5: SMTP set up and a test email delivered; password reset works; the sender domain's SPF and DKIM are verified
- [ ] §6.6: billing job heartbeats set up, and each one tested; 🚧 uptime, error and other job alerts set up; each
  alert pointing at a named person
- [ ] §7: every job scheduled and run once; times chosen and written down
- [ ] A restore from backup tested
- [ ] The door firmware tested against the new hostname (the `tests/door_compat/` suite passes)
- [ ] The import rehearsed; the report is clean or every line explained
- [ ] ❓ `--mark-inducted` decision recorded
- [ ] Old system's nightly jobs ready to be turned off at cutover; old webhooks ready to be repointed
- [ ] §6.7: Moodle sign-in and course checks rehearsed on a copy of Moodle; Moodle's old database login and access removed

---

## 12. Who owns what

Fill this in. It's the first thing needed if something breaks.

| Account / secret | Owner | Second owner | Where the credentials are |
|---|---|---|---|
| DigitalOcean | `<name>` | `<name>` | `<password manager entry>` |
| GitHub repository | `<name>` | `<name>` | |
| Domain (DNS) | `<name>` | `<name>` | |
| GoCardless | `<name>` | `<name>` | |
| Stripe | `<name>` | `<name>` | |
| PayPal | `<name>` | `<name>` | |
| Discord application and bot | `<name>` | `<name>` | |
| Email service | `<name>` | `<name>` | |
| Monitoring alerts go to | `<name>` | `<name>` | |
| `ID_DOCUMENT_KEYS` backup | `<name>` | `<name>` | `<password manager entry>` |

---

## 13. What isn't built yet (summary)

Everything marked 🚧 above, in one place, so the work can be planned:

1. Error tracking, heartbeats for the jobs other than billing, uptime monitor (§6.6)
2. `.do/app.yaml` for the app and jobs, and a way to deploy only after CI passes (§3.3)
3. The health check and HTTPS redirect check, with an exemption for `/healthz` if needed (§3.3)
4. The hourly proposals tally (§7), equipment photo and receipt storage (§3.2), the bank statement import (§5.4)
5. A staging app and the shadow site (§9)
6. How the import reaches the legacy database (§8), and the legacy photo bucket's name
7. CI's `check --deploy --fail-level WARNING` step (`.github/workflows/ci.yml`) fails while GoCardless, Stripe, PayPal,
   Discord, Spaces and the billing heartbeats are unset, because each deploy check warns. Give CI throwaway values, or silence those checks
   there, so the step passes again.
