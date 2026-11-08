"""
Heartbeats for the nightly billing jobs (spec 01 §1.8, 03 §3.6).

Each job pings its own URL at a monitoring service such as Healthchecks.io: `<url>/start` as it begins, `<url>` when
it finishes and `<url>/fail` if it crashes. The service alerts a named person when a ping doesn't arrive on time, so
a night where the job never ran is noticed as well as one where it failed. The URLs are set per job
(`HEARTBEAT_CREATE_CHARGES`, ...); a job without one sends nothing.

A ping that can't be sent is logged and never stops or fails the job: billing matters more than monitoring.
Pings say only how many things the job did (the same summary line it prints), never who.
"""

import logging
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10


def url_for(job):
    return settings.JOB_HEARTBEATS.get(job, "").rstrip("/")


def ping(job, event="", message=""):
    """Send `event` ("start", "" for success, or "fail") for `job`. Returns True if the service answered."""
    url = url_for(job)
    if not url:
        return False
    if event:
        url = f"{url}/{event}"
    request = urllib.request.Request(
        url, data=message.encode()[:10_000], headers={"User-Agent": "BBMS job heartbeat"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS):
            return True
    except Exception as error:  # noqa: BLE001 - a monitoring outage must never stop billing
        logger.warning("Heartbeat for %s (%s) not sent: %s", job, event or "success", type(error).__name__)
        return False
