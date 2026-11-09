# The demo site, for trustees to try BBMS out

A copy of the new site with made-up data runs on the clone droplet (see `tools/clone_test/README.md` for the droplet
itself), at **https://<droplet-ip>:8443/**. It is an ordinary production build with one switch on, `DEMO_SITE=True`:

- every page has a yellow "Demo site" banner;
- the fakes that stand in for GoCardless, Stripe, PayPal, Discord and Moodle are on, as when developing, so paying,
  setting up Direct Debits and linking Discord all work without touching a real service;
- the fake devices are on: the machine reader at `/dev/fake-device/` and the exit node at `/dev/fake-exit/`. A link
  can carry a device's key after a `#` (`/dev/fake-device/#key=…`; the browser never sends that part to the server);
- `DEBUG` stays off, so errors don't show settings or code.

No emails leave the droplet (it has no internet access); they're written to the site's log
(`docker logs bbms-demo`).

## Deploying

From the checkout you want to show (anything in it, committed or not, minus what git ignores):

```bash
tools/demo_site/deploy.sh                 # update the site; the database and uploads are kept
tools/demo_site/deploy.sh --reset-data    # update, then wipe the data and seed it again (asks first)
```

The first deploy needs the members' shared password: `DEMO_PASSWORD='…' tools/demo_site/deploy.sh`. It's kept on the
droplet (`/root/bbms-demo/demo-password`) for later re-seeds.

What it does: starts the allowlisting proxy here and a reverse tunnel to it (the droplet has no internet), sends the
checkout and builds the image `bbms:demo` on the droplet, then runs `remote.sh` there, which:

1. makes the secrets once (`/root/bbms-demo/env` and `pg.env`, root-only, never printed);
2. starts Postgres (`bbms-demo-db`, `127.0.0.1:5434`, volume `bbms-demo-pgdata`) if it isn't running;
3. runs the migrations, and seeds the demo with `manage.py seed_demo` if the database has no members (the device keys
   are then copied back to `~/bbms-demo/keys.json` on your machine, and kept in `/root/bbms-demo/keys.json`);
4. restarts the site (`bbms-demo`, gunicorn on `127.0.0.1:8002`; uploads and the fakes' state in volume
   `bbms-demo-data`);
5. sets up nginx on port 8443 with a self-signed certificate for the address (`/etc/nginx/bbms-demo/`), opens 8443 in
   ufw, and installs the scheduled jobs (`/etc/cron.d/bbms-demo`, output in `/var/log/bbms-demo-jobs.log`).

Settings: `DEMO_HOST`, `DEMO_PORT`, `DEMO_SSH_KEY`, `DEMO_OUT` (see the top of `deploy.sh`).

The certificate is self-signed (there's no domain name, and the droplet can't reach Let's Encrypt), so browsers warn
once: *Advanced → Proceed*. The connection is still encrypted.

## The demo data

`apps/core/management/commands/seed_demo.py` (refuses to run unless `DEMO_SITE` or `DEBUG` is on, and only fills an
empty database): 24 members, all famous makers, scientists and engineers but Victor Frankenstein (banned),
all `@example.com`; five trustees with full admin rights; teams for each group of machines; 16 machines with readers
(the MIG welder is out of order) and a retired one with a history; three months of visits, sessions and payments;
expenses waiting for approval; an open proposal. Fob card numbers are 2026001 onwards, in the members' order.

## Removing it

```bash
docker rm -f bbms-demo bbms-demo-db; docker volume rm bbms-demo-pgdata bbms-demo-data; docker image rm bbms:demo
rm -r /root/bbms-demo /etc/nginx/bbms-demo /etc/cron.d/bbms-demo /etc/nginx/sites-enabled/bbms-demo \
  /etc/nginx/sites-available/bbms-demo; systemctl reload nginx; ufw delete allow 8443/tcp
```
