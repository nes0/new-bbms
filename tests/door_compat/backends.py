"""
Backends set up the test members/fobs on a particular BBMS server and let tests change them.

A backend must provide:
    name                      short id, e.g. "legacy-clone"
    start() / stop()          connect / disconnect (e.g. SSH tunnel)
    https_host, https_port    where the proxy should send HTTPS requests
    host_header               Host header BBMS expects
    verify_tls                whether the server has a certificate we can verify
    api_key, device_name      credentials of the test door device
    install_fixtures(people)  create the test members and fobs (see test_door_compat.PEOPLE)
    remove_fixtures()
    set_fob_active(person, bool)
    set_keyholder(person, bool)
    entries_recorded(person)  number of successful main-door entries BBMS logged for them
    last_boot()               the test device's last_boot timestamp (or None)

To run the same tests against the new system, add a backend for it here.
"""

import os
import secrets
import shlex
import socket
import subprocess
import time

FIXTURE_ID_BASE = 9000  # all fixture rows use ids above this, so clean-up can't touch real data


class LegacyCloneBackend:
    """The sanitized legacy Laravel clone, reached over SSH."""

    name = "legacy-clone"
    host_header = "bbms.buildbrighton.com"
    verify_tls = False  # the clone uses a self-signed certificate
    device_name = "door-compat-test"

    def __init__(self):
        self.ssh = shlex.split(
            os.environ.get(
                "BBMS_CLONE_SSH",
                "ssh -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 -i ~/.ssh/<ssh-key> root@<droplet-ip>",
            )
        )
        self.ssh = [os.path.expanduser(part) for part in self.ssh]
        self.api_key = "door-compat-" + secrets.token_hex(12)
        self.https_host = "127.0.0.1"
        self.https_port = None
        self._tunnel = None

    # -- connection --------------------------------------------------------------------
    def start(self):
        self.https_port = _free_port()
        self._tunnel = subprocess.Popen(
            self.ssh[:1]
            + ["-N", "-o", "ExitOnForwardFailure=yes", "-L", f"{self.https_port}:127.0.0.1:443"]
            + self.ssh[1:]
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._tunnel.poll() is not None:
                raise RuntimeError("SSH tunnel to the clone failed to start")
            try:
                socket.create_connection(("127.0.0.1", self.https_port), timeout=1).close()
                return
            except OSError:
                time.sleep(0.5)
        raise RuntimeError("SSH tunnel to the clone did not come up")

    def stop(self):
        if self._tunnel:
            self._tunnel.terminate()
            self._tunnel.wait(timeout=10)

    # -- fixtures ----------------------------------------------------------------------
    def install_fixtures(self, people):
        self.remove_fixtures()
        rows = ["SET SESSION sql_mode='';"]
        node_id = FIXTURE_ID_BASE + 1
        rows.append(
            "INSERT INTO acs_nodes (id, name, device_id, entry_device, monitor_heartbeat, api_key, "
            f"created_at, updated_at) VALUES ({node_id}, 'Door compatibility test', "
            f"{_q(self.device_name)}, 1, 0, {_q(self.api_key)}, NOW(), NOW());"
        )
        for person in people:
            uid = FIXTURE_ID_BASE + person.number
            rows.append(
                "INSERT INTO users (id, hash, given_name, family_name, email, status, active, trusted, "
                "key_holder, created_at, updated_at) VALUES "
                f"({uid}, {_q('doortest' + str(uid))}, {_q(person.given_name)}, {_q(person.family_name)}, "
                f"{_q(f'door-test-{person.number}@example.com')}, {_q(person.status)}, "
                f"{int(person.active)}, {int(person.trusted)}, {int(person.key_holder)}, NOW(), NOW());"
            )
            if person.has_profile:
                rows.append(
                    "INSERT INTO profile_data (user_id, profile_photo, profile_photo_on_wall, created_at, "
                    f"updated_at) VALUES ({uid}, {int(person.photo)}, {int(person.photo_on_wall)}, NOW(), NOW());"
                )
            if person.tag_id:
                rows.append(
                    "INSERT INTO key_fobs (id, user_id, key_id, active, lost, created_at, updated_at) VALUES "
                    f"({uid}, {uid}, {_q(person.tag_id)}, {int(person.fob_active)}, "
                    f"{int(not person.fob_active)}, NOW(), NOW());"
                )
        self._sql("\n".join(rows))

    def remove_fixtures(self):
        b = FIXTURE_ID_BASE
        self._sql(
            f"DELETE FROM access_log WHERE user_id > {b};"
            f"DELETE FROM key_fobs WHERE id > {b};"
            f"DELETE FROM profile_data WHERE user_id > {b};"
            f"DELETE FROM users WHERE id > {b};"
            f"DELETE FROM acs_nodes WHERE id > {b} OR device_id = {_q(self.device_name)};"
        )

    def set_fob_active(self, person, active: bool):
        uid = FIXTURE_ID_BASE + person.number
        self._sql(f"UPDATE key_fobs SET active={int(active)}, lost={int(not active)} WHERE id={uid};")

    def set_keyholder(self, person, key_holder: bool):
        uid = FIXTURE_ID_BASE + person.number
        self._sql(f"UPDATE users SET key_holder={int(key_holder)} WHERE id={uid};")

    def entries_recorded(self, person) -> int:
        uid = FIXTURE_ID_BASE + person.number
        out = self._sql(
            f"SELECT COUNT(*) FROM access_log WHERE user_id={uid} AND service='main-door' AND response = '200';"
        )
        return int(out.strip() or 0)

    def last_boot(self):
        out = self._sql(f"SELECT IFNULL(last_boot, '') FROM acs_nodes WHERE device_id={_q(self.device_name)};")
        return out.strip() or None

    # -- internals ---------------------------------------------------------------------
    def _sql(self, sql: str) -> str:
        result = subprocess.run(
            self.ssh + ["mysql", "-N", "bbms"], input=sql, text=True, capture_output=True, timeout=60
        )
        if result.returncode != 0:
            raise RuntimeError(f"SQL on clone failed: {result.stderr.strip()}")
        return result.stdout


def _q(value: str) -> str:
    """Quote a string for MySQL (fixture values are our own constants)."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DjangoLocalBackend:
    """The new Django site, run locally with `manage.py runserver` on a throwaway SQLite database.

    Test members and fobs are created directly through the Django ORM, in this process, on the same database
    file the server uses.
    """

    name = "django-local"
    host_header = "localhost"
    verify_tls = False
    use_tls = False  # the development server speaks plain HTTP
    matches_card_number = True  # spec 05 §5.1
    device_name = "door-compat-test"

    def __init__(self):
        self.api_key = "door-compat-" + secrets.token_hex(12)
        self.https_host = "127.0.0.1"
        self.https_port = None
        self._server = None
        self._tmp = None

    # -- connection --------------------------------------------------------------------
    def start(self):
        import sys
        import tempfile
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        self._tmp = tempfile.mkdtemp(prefix="door-compat-django-")
        env = {
            "DEBUG": "True",
            "SECRET_KEY": "door-compat-only",
            "ALLOWED_HOSTS": "localhost,127.0.0.1",
            "DATABASE_URL": f"sqlite:///{self._tmp}/db.sqlite3",
            "DB_CONN_MAX_AGE": "0",
            "DJANGO_SETTINGS_MODULE": "bbms.settings",
        }
        os.environ.update(env)
        sys.path.insert(0, str(repo_root))
        import django
        from django.core.management import call_command

        django.setup()
        call_command("migrate", verbosity=0)

        self.https_port = _free_port()
        self._server = subprocess.Popen(
            [sys.executable, "manage.py", "runserver", f"127.0.0.1:{self.https_port}", "--noreload"],
            cwd=repo_root,
            env={**os.environ, **env},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._server.poll() is not None:
                raise RuntimeError("Django development server failed to start")
            try:
                socket.create_connection(("127.0.0.1", self.https_port), timeout=1).close()
                return
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("Django development server did not come up")

    def stop(self):
        import shutil

        if self._server:
            self._server.terminate()
            self._server.wait(timeout=10)
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)

    # -- fixtures ----------------------------------------------------------------------
    def install_fixtures(self, people):
        from apps.access.models import Device, KeyFob
        from apps.members.models import Member

        door = Device(name="Door compatibility test", device_id=self.device_name, role=Device.Role.ENTRY)
        door.set_api_key(self.api_key)
        door.save()
        for person in people:
            member = Member.objects.create_user(
                f"door-test-{person.number}@example.com",
                "door-compat-password",
                given_name=person.given_name,
                family_name=person.family_name,
                status=Member.Status.ACTIVE if person.active else Member.Status.LEFT,
                trusted=person.trusted,
                key_holder=person.key_holder,
                # The legacy "no profile row" case has no photo information at all.
                photo_approved=person.photo and person.has_profile,
                photo_on_wall=person.photo_on_wall and person.has_profile,
            )
            if person.tag_id:
                KeyFob.objects.create(member=member, tag_id=person.tag_id, active=person.fob_active)

    def remove_fixtures(self):
        pass  # the whole database is thrown away in stop()

    def _member(self, person):
        from apps.members.models import Member

        return Member.objects.get(email=f"door-test-{person.number}@example.com")

    def set_fob_active(self, person, active: bool):
        from apps.access.models import KeyFob

        KeyFob.objects.filter(member=self._member(person)).update(active=active)

    def set_keyholder(self, person, key_holder: bool):
        member = self._member(person)
        member.key_holder = key_holder
        member.save(update_fields=["key_holder"])

    def entries_recorded(self, person) -> int:
        from apps.access.models import AccessEvent

        return AccessEvent.objects.filter(member=self._member(person), kind=AccessEvent.Kind.ENTRY).count()

    def last_boot(self):
        from apps.access.models import Device

        return Device.objects.get(device_id=self.device_name).last_boot


BACKENDS = {"legacy-clone": LegacyCloneBackend, "django-local": DjangoLocalBackend}
