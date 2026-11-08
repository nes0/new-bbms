"""The member's online training page (spec 06 §6.12, "Linking equipment to courses")."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.equipment.models import Equipment, Induction, TrainingCompletion

from . import client, completion

PAUSE_SECONDS = 30  # between "check now" presses for one course: courtesy to Moodle


@login_required
def training(request):
    """Every course BBMS knows about, the machines it unlocks, and whether the member has finished it."""
    member = request.user
    courses = {}
    for equipment in Equipment.objects.filter(moodle_course_id__isnull=False, archived=False).order_by("name"):
        courses.setdefault(equipment.moodle_course_id, []).append(equipment)
    done = {
        row.moodle_course_id: row for row in TrainingCompletion.objects.filter(member=member, cleared_at__isnull=True)
    }
    trained = {
        induction.category for induction in Induction.objects.filter(member=member, paid=True, trained_at__isnull=False)
    }
    rows = [
        {
            "course_id": course_id,
            "machines": machines,
            "link": client.course_link(course_id),
            "completion": done.get(course_id),
            "signed_off": any(machine.induction_key in trained for machine in machines),
        }
        for course_id, machines in sorted(courses.items())
    ]
    return render(request, "moodle/training.html", {"rows": rows, "can_check": client.enabled()})


@login_required
@require_POST
def check_now(request, course_id):
    """ "I've finished the course, check now"."""
    if not Equipment.objects.filter(moodle_course_id=course_id, archived=False).exists():
        messages.error(request, "That isn't one of our online courses.")
        return redirect("moodle:training")
    if not client.enabled():
        messages.error(request, "We can't check online courses at the moment. Please ask a trainer.")
        return redirect("moodle:training")
    if not cache.add(f"moodle-check:{request.user.pk}:{course_id}", 1, PAUSE_SECONDS):
        messages.info(request, "We've only just checked. Please wait half a minute and try again.")
        return redirect("moodle:training")
    outcome = completion.check(request.user, course_id)
    text = {
        completion.COMPLETED: (messages.success, "Well done: we've recorded that you've finished the course."),
        completion.ALREADY: (messages.info, "We already have that you've finished the course."),
        completion.NOT_YET: (messages.info, "Moodle doesn't show the course as finished yet."),
        completion.NO_ACCOUNT: (
            messages.info,
            'Moodle doesn\'t know you yet. Log in there with "Log in with BBMS", then take the course.',
        ),
        completion.UNAVAILABLE: (messages.error, "We couldn't reach Moodle just now. Please try again later."),
    }[outcome]
    text[0](request, text[1])
    return redirect("moodle:training")
