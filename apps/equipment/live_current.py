"""
Live current: a machine's reader sends its current readings every few seconds while an admin watches them, to choose
the machine's threshold (05 §5.4). Reporting is switched on for a while from the machine's "Live current" admin page
and stops by itself, so a page left open or forgotten doesn't keep the reader busy.
"""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .models import CurrentSample, Equipment

REPORT_FOR = timedelta(minutes=15)
SAMPLE_SECONDS = 1  # the reader measures once a second and sends what it measured since its last report
MAX_SAMPLES = 60  # per report
MAX_AMPS = 1000


def is_reporting(equipment: Equipment, now=None) -> bool:
    until = equipment.report_current_until
    return until is not None and until > (now or timezone.now())


def start_reporting(equipment: Equipment, now=None) -> None:
    """Report for the next REPORT_FOR (again, if it already is). Old samples are cleared out here."""
    now = now or timezone.now()
    equipment.report_current_until = now + REPORT_FOR
    equipment.save(update_fields=["report_current_until"])
    CurrentSample.objects.filter(measured_at__lt=now - CurrentSample.KEEP_FOR).delete()


def stop_reporting(equipment: Equipment) -> None:
    equipment.report_current_until = None
    equipment.save(update_fields=["report_current_until"])


def record(equipment: Equipment, device, amps: list, now=None) -> None:
    """Store a report's samples, oldest first: the last was measured just now, the others a second apart."""
    now = now or timezone.now()
    count = len(amps)
    CurrentSample.objects.bulk_create(
        CurrentSample(
            equipment=equipment,
            device=device,
            amps=Decimal(str(round(value, 2))),
            measured_at=now - timedelta(seconds=SAMPLE_SECONDS * (count - 1 - i)),
        )
        for i, value in enumerate(amps)
    )


def snapshot(equipment: Equipment, minutes=5, now=None) -> dict:
    """What the admin page shows: the last few minutes of samples and the machine's settings."""
    now = now or timezone.now()
    samples = list(
        equipment.current_samples.filter(measured_at__gte=now - timedelta(minutes=minutes))
        .order_by("measured_at")
        .values_list("measured_at", "amps")
    )
    last_minute = [amps for at, amps in samples if at >= now - timedelta(minutes=1)]
    threshold = equipment.current_threshold_amps
    return {
        "now": int(now.timestamp() * 1000),
        "window_ms": minutes * 60 * 1000,
        "reporting": is_reporting(equipment, now),
        "until": int(equipment.report_current_until.timestamp() * 1000) if is_reporting(equipment, now) else None,
        "threshold": float(threshold) if threshold is not None else None,
        "samples": [[int(at.timestamp() * 1000), float(amps)] for at, amps in samples],
        "last_minute": {"min": float(min(last_minute)), "max": float(max(last_minute))} if last_minute else None,
        "last_sample_at": int(samples[-1][0].timestamp() * 1000) if samples else None,
    }
