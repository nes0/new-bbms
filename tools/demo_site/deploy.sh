#!/bin/bash
# Deploy (or redeploy) the BBMS demo site to the clone droplet. Run from anywhere in the repo, on your machine:
#
#   tools/demo_site/deploy.sh                 # build this checkout as it stands and update the site; data is kept
#   tools/demo_site/deploy.sh --reset-data    # the same, then start again from freshly seeded data (asks first)
#
# It sends the files of the checkout you run it from (committed or not, minus what git ignores), so check out what you
# want the trustees to see first.
#
# First deploy only: DEMO_PASSWORD='…' tools/demo_site/deploy.sh   (the password every demo member shares)
#
# Settings (environment): DEMO_HOST (required), DEMO_PORT (8443), DEMO_SSH_KEY (required),
# DEMO_OUT (~/bbms-demo: where the device keys are copied to after seeding).
#
# The droplet has no internet access, so this runs tools/clone_test/allowproxy.py here and gives the droplet a
# reverse tunnel to it while the image builds (Docker Hub and PyPI only); both stop when the script ends.
set -euo pipefail

RESET=${1:-}
HOST=${DEMO_HOST:?set DEMO_HOST to the droplet's address}
PORT=${DEMO_PORT:-8443}
KEY=${DEMO_SSH_KEY:?set DEMO_SSH_KEY to the ssh key for the droplet}
OUT=${DEMO_OUT:-$HOME/bbms-demo}
PROXY_PORT=31280
cd "$(git rev-parse --show-toplevel)"
COMMIT=$(git rev-parse --short HEAD)$([ -z "$(git status --porcelain)" ] || echo "+changes")
SSH=(ssh -o IdentitiesOnly=yes -o ServerAliveInterval=30 -i "$KEY")

if [ "$RESET" = "--reset-data" ]; then
  read -r -p "Delete the demo site's database and uploads on $HOST and seed it again? [y/N] " answer
  [ "$answer" = y ] || { echo "Nothing done."; exit 1; }
elif [ -n "$RESET" ]; then
  echo "Unknown option: $RESET" >&2; exit 2
fi

PIDS=()
cleanup() { for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT

echo "--- Opening the build proxy and the tunnel"
if ! ss -ltn | grep -q "127.0.0.1:$PROXY_PORT "; then
  python3 tools/clone_test/allowproxy.py "$PROXY_PORT" > "${TMPDIR:-/tmp}/bbms-demo-proxy.log" 2>&1 &
  PIDS+=($!)
fi
"${SSH[@]}" -N -o ExitOnForwardFailure=yes -R "127.0.0.1:$PROXY_PORT:127.0.0.1:$PROXY_PORT" "root@$HOST" &
PIDS+=($!)
sleep 3

echo "--- Building $COMMIT on the droplet (image bbms:demo)"
git ls-files -co --exclude-standard -z | tar --null -T - -cf - | "${SSH[@]}" "root@$HOST" \
  "DOCKER_BUILDKIT=0 docker build -q --network host --build-arg HTTPS_PROXY=http://127.0.0.1:$PROXY_PORT \
   --label bbms.commit=$COMMIT -t bbms:demo - >/dev/null && echo built"

echo "--- Updating the site"
if [ -n "${DEMO_PASSWORD:-}" ]; then
  # Sent on stdin's first line, so it never appears in a command line.
  { printf '%s\n' "$DEMO_PASSWORD"; cat tools/demo_site/remote.sh; } | "${SSH[@]}" "root@$HOST" \
    "read -r DEMO_PASSWORD; export DEMO_PASSWORD; bash -s -- $HOST $PORT $RESET"
else
  "${SSH[@]}" "root@$HOST" "bash -s -- $HOST $PORT $RESET" < tools/demo_site/remote.sh
fi
docker_image_prune="docker image prune -f >/dev/null"
"${SSH[@]}" "root@$HOST" "$docker_image_prune"

if "${SSH[@]}" "root@$HOST" test -f /root/bbms-demo/keys.json; then
  mkdir -p "$OUT" && chmod 700 "$OUT"
  "${SSH[@]}" "root@$HOST" cat /root/bbms-demo/keys.json > "$OUT/keys.json"
  echo "--- Device keys and fobs copied to $OUT/keys.json"
fi
echo "--- Done: https://$HOST:$PORT/ is running $COMMIT"
