"""What members are told about their machine sessions (spec 05 §5.6)."""

from django.utils import timezone

from apps.notifications.notify import notify


def _time(moment):
    return timezone.localtime(moment).strftime("%-d %b %H:%M") if moment else "not ended"


def session_adjusted(session, before, correction=None):
    """An admin changed a session's times. `before` is (started_at, ended_at) as they were; `correction` is the
    ledger row for any change to the charge (positive: charged more; negative: given back)."""
    old_start, old_end = before
    if (old_start, old_end) == (session.started_at, session.ended_at):
        return  # the form was saved with the same times
    message = (
        f"An admin adjusted your {session.equipment.name} session. "
        f"It was {_time(old_start)} to {_time(old_end)}; now it's {_time(session.started_at)} to "
        f"{_time(session.ended_at)}."
    )
    if correction is not None and correction.corrects_id and correction.amount_pence:
        pounds = f"£{abs(correction.amount_pence) / 100:.2f}"
        message += f" You've been {'refunded' if correction.amount_pence < 0 else 'charged'} {pounds}."
    notify(session.member, message, kind="session_adjusted", url="/credit/")
