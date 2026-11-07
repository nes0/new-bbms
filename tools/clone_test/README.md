# Testing the Docker image on the legacy clone droplet

The sanitized legacy clone (see the project notes for its address and SSH key) can also run the **new** site in
Docker, next to the legacy one, with the clone's data imported. Set up on 2026-11-07. Everything new listens on
`127.0.0.1` only; you reach it through an SSH tunnel.

## What's on the droplet

| Thing | Where |
|---|---|
| Docker 25 (static binaries, so the old apt sources aren't involved) | `/opt/docker-static`, linked into `/usr/local/bin`; service `docker.service` |
| Docker settings: doesn't touch the firewall (`iptables`, `ip-forward`, `ip-masq` off) | `/etc/docker/daemon.json` |
| Secrets for the test site (generated there, readable by root only) | `/root/bbms-test/env`, `/root/bbms-test/pg.env` |
| The key that encrypts proofs of identity, `ID_DOCUMENT_KEYS` (spec 03 §3.1); without it, uploads are switched off | in `env` (add it if it's missing) |
| Read-only MySQL user `bbms_readonly@127.0.0.1` (SELECT on `bbms` only) | its password is in `env` |
| Postgres 16 container `bbms-db`, data in volume `bbms-pgdata` | `127.0.0.1:5433` |
| The new site, container `bbms-web` (image `bbms:test`) | `127.0.0.1:8001` |

Containers use the host's network (`--network host`), so ufw still decides what's reachable from outside, and it
only allows 22, 80 and 443.

## The droplet has no internet access

That's deliberate. To pull images and Python packages, run the allowlisting proxy **on your machine** and give the
droplet a reverse tunnel to it while you work. The proxy only lets through Docker Hub and PyPI, and logs every
request it allows or refuses. Docker on the droplet is set to use it (`HTTPS_PROXY` in the service).

```bash
python3 tools/clone_test/allowproxy.py 31280 &                       # on your machine
ssh -i <key> -N -R 127.0.0.1:31280:127.0.0.1:31280 root@<clone> &    # the tunnel; stop it when you're done
```

## Build, import and run

From the repo root on your machine (sends the working tree, minus ignored files, as the build context):

```bash
git ls-files -co --exclude-standard -z | tar --null -T - -cf - | ssh -i <key> root@<clone> \
  'DOCKER_BUILDKIT=0 docker build --network host --build-arg HTTPS_PROXY=http://127.0.0.1:31280 -t bbms:test -'
```

Then on the droplet (`cd /root/bbms-test`):

```bash
R="docker run --rm --network host --env-file env bbms:test"
$R python manage.py test --noinput            # the whole test suite, on Postgres
$R python manage.py migrate --noinput
$R python manage.py import_legacy --dry-run   # report only
$R python manage.py import_legacy             # needs an empty database; to redo it, see below
docker rm -f bbms-web; docker run -d --name bbms-web --restart unless-stopped --network host --env-file env \
  bbms:test gunicorn bbms.wsgi:application --bind 127.0.0.1:8001 --workers 2 --access-logfile -
```

To import again from scratch: `$R python manage.py flush --noinput`, then `import_legacy`.

## Look at it

```bash
ssh -i <key> -N -L 8001:127.0.0.1:8001 root@<clone>
```

Open <http://localhost:8001>. The clone's test logins work with their legacy passwords (see the project notes).

## Removing it all

```bash
docker rm -f bbms-web bbms-db; docker volume rm bbms-pgdata; docker image prune -af
systemctl disable --now docker; rm /etc/systemd/system/docker.service /etc/docker/daemon.json
rm -r /opt/docker-static /root/bbms-test; find /usr/local/bin -lname '/opt/docker-static/*' -delete
mysql -e "DROP USER 'bbms_readonly'@'127.0.0.1'"
```
