"""
Asking Moodle questions (spec 06 §6.12, "Checking completion").

BBMS uses Moodle's web services with a dedicated account's token (`MOODLE_TOKEN`, a secret), and only ever asks two
things: which Moodle user is this member, and has that user completed this course. Machine access never calls Moodle:
it uses the completions BBMS has stored, so a Moodle outage can't block a machine.

`client()` returns one of:
- `MoodleApi`: the real one, when Moodle's address (admin Settings) and `MOODLE_TOKEN` are both set;
- `FakeMoodle`: when developing or testing without them (nothing leaves the machine);
- `NotConfigured`: a live site without them. Every question raises `MoodleUnavailable`.
Tests can put their own in place with `use()`.
"""

import contextlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

from django.conf import settings

from apps.site_settings.models import SiteSettings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15


class MoodleError(Exception):
    """Moodle answered, but with an error (a bad token, the course has no completion tracking, and so on)."""


class MoodleUnavailable(MoodleError):
    """Moodle couldn't be reached, or isn't set up. The caller tries again later."""


@dataclass
class CourseStatus:
    completed: bool
    completed_at: datetime | None = None  # when Moodle says they finished, if it says


def moodle_url() -> str:
    return SiteSettings.load().moodle_url.rstrip("/")


def configured() -> bool:
    return bool(moodle_url() and settings.MOODLE_TOKEN)


def using_fake() -> bool:
    return not configured() and (settings.DEBUG or getattr(settings, "TESTING", False))


def enabled() -> bool:
    """Whether BBMS can check courses at all (so the page can offer "check now")."""
    return configured() or using_fake()


def course_link(course_id: int) -> str:
    """Where a member takes the course. Empty if Moodle's address isn't set."""
    base = moodle_url()
    return f"{base}/course/view.php?id={course_id}" if base else ""


class MoodleApi:
    """The real web services, over plain HTTP."""

    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _call(self, function, **params):
        form = {"wstoken": self.token, "wsfunction": function, "moodlewsrestformat": "json", **params}
        request = urllib.request.Request(
            f"{self.base_url}/webservice/rest/server.php",
            data=urllib.parse.urlencode(form).encode(),
            headers={"User-Agent": "BBMS", "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 (admin-set https URL)
                payload = json.loads(response.read() or b"null")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise MoodleUnavailable(f"Couldn't reach Moodle: {type(error).__name__}") from error
        # Moodle answers 200 with {"exception": ..., "errorcode": ..., "message": ...} for a failed call.
        if isinstance(payload, dict) and "exception" in payload:
            raise MoodleError(f"Moodle refused {function}: {payload.get('errorcode', 'unknown')}")
        return payload

    def find_user_id(self, member) -> int | None:
        """The member's Moodle user id: by ID number (their BBMS id), else by email address. None if no such user."""
        for field, value in (("idnumber", str(member.pk)), ("email", member.email.lower())):
            users = self._call("core_user_get_users_by_field", field=field, **{"values[0]": value})
            if users:
                return int(users[0]["id"])
        return None

    def course_status(self, moodle_user_id, course_id) -> CourseStatus:
        payload = self._call("core_completion_get_course_completion_status", courseid=course_id, userid=moodle_user_id)
        status = (payload or {}).get("completionstatus") or {}
        completed = bool(status.get("completed"))
        times = [c.get("timecompleted") for c in status.get("completions", []) if c.get("timecompleted")]
        when = datetime.fromtimestamp(max(times), UTC) if completed and times else None
        return CourseStatus(completed, when)


class FakeMoodle:
    """Stands in for Moodle in development and tests: nothing is sent anywhere."""

    def __init__(self):
        self.users = {}  # BBMS member id -> Moodle user id
        self.completed = {}  # (Moodle user id, course id) -> when they finished (or None)
        self.down = False  # True: every question fails, as in an outage
        self.calls = []

    def add_user(self, member, moodle_user_id=100):
        self.users[member.pk] = moodle_user_id
        return moodle_user_id

    def complete(self, member, course_id, when=None):
        self.completed[(self.users[member.pk], course_id)] = when

    def find_user_id(self, member):
        self.calls.append(("find_user_id", member.pk))
        if self.down:
            raise MoodleUnavailable("Moodle is down")
        return self.users.get(member.pk)

    def course_status(self, moodle_user_id, course_id):
        self.calls.append(("course_status", moodle_user_id, course_id))
        if self.down:
            raise MoodleUnavailable("Moodle is down")
        key = (moodle_user_id, course_id)
        return CourseStatus(key in self.completed, self.completed.get(key))


class NotConfigured:
    def find_user_id(self, member):
        raise MoodleUnavailable("Moodle isn't set up")

    def course_status(self, moodle_user_id, course_id):
        raise MoodleUnavailable("Moodle isn't set up")


_fake = FakeMoodle()
_override = []


@contextlib.contextmanager
def use(replacement):
    """Use this in place of Moodle (tests)."""
    _override.append(replacement)
    try:
        yield replacement
    finally:
        _override.pop()


def client():
    if _override:
        return _override[-1]
    if configured():
        return MoodleApi(moodle_url(), settings.MOODLE_TOKEN)
    return _fake if using_fake() else NotConfigured()
