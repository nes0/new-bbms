#!/bin/bash
# Runs ON THE DROPLET, sent by deploy.sh (as root, through ssh). Sets up or updates the demo site; safe to run again.
#   remote.sh <public address> <https port> [--reset-data]
# Keeps the database and uploads unless --reset-data is given (then the site starts again from freshly seeded data).
set -euo pipefail

ADDRESS=$1
PORT=$2
RESET=${3:-}
DIR=/root/bbms-demo
IMAGE=bbms:demo
WEB=bbms-demo          # gunicorn on 127.0.0.1:8002
DB=bbms-demo-db        # Postgres on 127.0.0.1:5434
umask 077
mkdir -p "$DIR"
cd "$DIR"
say() { echo "--- $*"; }

# -- Secrets, made here once and never printed -----------------------------------------------------------------------
if [ ! -f pg.env ]; then
  say "Making the database password"
  echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)" > pg.env
fi
PGPASS=$(sed -n 's/^POSTGRES_PASSWORD=//p' pg.env)
if [ ! -f env ]; then
  say "Making the site's settings ($DIR/env)"
  FERNET=$(docker run --rm "$IMAGE" python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  cat > env <<ENV
DEMO_SITE=True
DEBUG=False
SECRET_KEY=$(openssl rand -hex 40)
ID_DOCUMENT_KEYS=$FERNET
DATABASE_URL=postgres://bbms:$PGPASS@127.0.0.1:5434/bbms_demo
ALLOWED_HOSTS=$ADDRESS,127.0.0.1,localhost
CSRF_TRUSTED_ORIGINS=https://$ADDRESS:$PORT
SITE_URL=https://$ADDRESS:$PORT
SECURE_HSTS_SECONDS=0
CLIENT_IP_HEADER=HTTP_X_REAL_IP
MEDIA_ROOT=/data/media
FAKES_DIR=/data/fakes
ENV
fi
# Settings added since the env file was first made (appended if missing; existing values are kept).
for setting in SESSION_COOKIE_NAME=bbms_demo_session CSRF_COOKIE_NAME=bbms_demo_csrf; do
  grep -q "^${setting%%=*}=" env || echo "$setting" >> env
done
if [ ! -f demo-password ]; then
  [ -n "${DEMO_PASSWORD:-}" ] || { echo "First deploy: set DEMO_PASSWORD (the members' shared password)." >&2; exit 1; }
  printf '%s' "$DEMO_PASSWORD" > demo-password
fi

# -- Starting again from scratch, if asked ----------------------------------------------------------------------------
if [ "$RESET" = "--reset-data" ]; then
  say "Deleting the demo's database and uploads (--reset-data)"
  docker rm -f "$WEB" "$DB" >/dev/null 2>&1 || true
  docker volume rm bbms-demo-pgdata bbms-demo-data >/dev/null 2>&1 || true
  rm -f keys.json
fi

# -- Database --------------------------------------------------------------------------------------------------------
if ! docker ps --format '{{.Names}}' | grep -qx "$DB"; then
  docker rm -f "$DB" >/dev/null 2>&1 || true
  say "Starting Postgres ($DB)"
  docker run -d --name "$DB" --restart unless-stopped --network host --env-file pg.env \
    -e POSTGRES_USER=bbms -e POSTGRES_DB=bbms_demo -v bbms-demo-pgdata:/var/lib/postgresql/data \
    postgres:16-alpine -c listen_addresses=127.0.0.1 -c port=5434 >/dev/null
fi
for _ in $(seq 60); do docker exec "$DB" pg_isready -q -h 127.0.0.1 -p 5434 && break; sleep 1; done

# -- Uploads and the fakes' state live in a volume, so a redeploy keeps them ------------------------------------------
docker volume create bbms-demo-data >/dev/null
docker run --rm --user root -v bbms-demo-data:/data "$IMAGE" sh -c 'mkdir -p /data/media /data/fakes && chown -R bbms /data'

RUN="docker run --rm --network host --env-file env -v bbms-demo-data:/data $IMAGE"
say "Migrating"
$RUN python manage.py migrate --noinput | grep -v '^  No migrations' || true

if [ "$($RUN python manage.py shell --no-imports -c 'from apps.members.models import Member; print(Member.objects.count())' | tail -n 1)" = 0 ]; then
  say "Empty database: seeding the demo (log in $DIR/seed.log)"
  DEMO_PASSWORD=$(cat demo-password) docker run --rm --network host --env-file env -v bbms-demo-data:/data \
    -e DEMO_PASSWORD "$IMAGE" python manage.py seed_demo --keys-file /data/keys.json > seed.log 2>&1 \
    || { tail -30 seed.log; exit 1; }
  docker run --rm -v bbms-demo-data:/data "$IMAGE" cat /data/keys.json > keys.json
  docker run --rm --user root -v bbms-demo-data:/data "$IMAGE" rm /data/keys.json
fi

# -- The site ----------------------------------------------------------------------------------------------------------
say "Starting the site ($WEB)"
docker rm -f "$WEB" >/dev/null 2>&1 || true
docker run -d --name "$WEB" --restart unless-stopped --network host --env-file env -v bbms-demo-data:/data \
  "$IMAGE" gunicorn bbms.wsgi:application --bind 127.0.0.1:8002 --workers 3 --timeout 60 --access-logfile - >/dev/null

# -- HTTPS: nginx on $PORT with a self-signed certificate for the address ----------------------------------------------
CERTS=/etc/nginx/bbms-demo
if [ ! -f "$CERTS/cert.pem" ]; then
  say "Making a self-signed certificate for $ADDRESS"
  mkdir -p "$CERTS"
  cat > "$CERTS/openssl.cnf" <<CNF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $ADDRESS
O = Build Brighton BBMS demo
[ext]
subjectAltName = IP:$ADDRESS
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
CNF
  openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 365 -config "$CERTS/openssl.cnf" \
    -keyout "$CERTS/key.pem" -out "$CERTS/cert.pem" 2>/dev/null
fi
cat > /etc/nginx/sites-available/bbms-demo <<NGINX
# The BBMS demo site (tools/demo_site in the BBMS repo). Proxies to gunicorn in the bbms-demo container.
server {
    listen $PORT ssl http2;
    listen [::]:$PORT ssl http2;
    server_name _;
    ssl_certificate $CERTS/cert.pem;
    ssl_certificate_key $CERTS/key.pem;
    ssl_protocols TLSv1.2;
    client_max_body_size 25m;
    location / {
        proxy_pass http://127.0.0.1:8002;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_read_timeout 60s;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/bbms-demo /etc/nginx/sites-enabled/bbms-demo
nginx -t -q && systemctl reload nginx

# -- Firewall: open the demo's port -----------------------------------------------------------------------------------
ufw status | grep -q "^$PORT/tcp .*ALLOW" || { say "Opening port $PORT in ufw"; ufw allow "$PORT/tcp" >/dev/null; }

# -- Scheduled jobs (spec deployment §6.4), as on the live site, against the fakes -------------------------------------
cat > /etc/cron.d/bbms-demo <<'CRON'
# BBMS demo site's scheduled jobs (tools/demo_site). Output: /var/log/bbms-demo-jobs.log
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
J="docker exec bbms-demo python manage.py"
L=/var/log/bbms-demo-jobs.log
0 1 * * *     root $J create_charges >> $L 2>&1
30 1 * * *    root $J bill_members >> $L 2>&1
0 6 * * *     root $J check_memberships >> $L 2>&1
0 7 * * *     root $J retry_direct_debits >> $L 2>&1
*/15 * * * *  root $J process_gocardless_events >> $L 2>&1; $J check_devices >> $L 2>&1; $J check_moodle_completions >> $L 2>&1
5 * * * *     root $J tidy_equipment_sessions >> $L 2>&1; $J update_proposals >> $L 2>&1
0 3 * * *     root $J shred_stale_id_documents >> $L 2>&1; $J sync_discord >> $L 2>&1; $J import_payouts >> $L 2>&1
50 23 * * *   root $J snapshot_members >> $L 2>&1
CRON

# -- Check it ----------------------------------------------------------------------------------------------------------
for _ in $(seq 30); do
  curl -skf "https://127.0.0.1:$PORT/healthz" -H "Host: $ADDRESS:$PORT" >/dev/null && break
  sleep 1
done
curl -skf "https://127.0.0.1:$PORT/healthz" -H "Host: $ADDRESS:$PORT" >/dev/null \
  && say "Up: https://$ADDRESS:$PORT/" || { echo "The site didn't answer; docker logs $WEB:" >&2; docker logs --tail 40 "$WEB" >&2; exit 1; }
