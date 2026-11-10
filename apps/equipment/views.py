"""The members' equipment pages (spec 05 §5.7): the list, each machine's page, and what can be done there."""

from datetime import date

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.core.markup import to_html
from apps.group_email import audiences as group_email
from apps.moodle import client as moodle
from apps.profiles.views import is_member

from . import lifetime, pages
from .models import Equipment, UsageSession

SESSIONS_PER_PAGE = 25


def _members_only(request):
    """Members only, as legacy (its equipment pages needed the member role): the list shows who is trained."""
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next={request.path}")
    if not is_member(request.user):
        raise Http404
    return None


def can_review(user) -> bool:
    """Who sees retired equipment and its lifetime figures: admins, and the finance pages' readers (04 §4.8)."""
    return user.is_authenticated and (
        user.has_perm("equipment.change_equipment")
        or user.has_perm("finance.see_finance")
        or user.has_perm("finance.manage_finance")
    )


def index(request):
    if (response := _members_only(request)) is not None:
        return response
    member = request.user
    rows = [(machine, pages.training_state(member, machine)) for machine in pages.visible_equipment()]
    context = {
        "needs_training": [row for row in rows if row[0].requires_induction],
        "open_to_all": [row for row in rows if not row[0].requires_induction],
        "can_review": can_review(member),
    }
    return render(request, "equipment/index.html", context)


def detail(request, slug):
    if (response := _members_only(request)) is not None:
        return response
    member = request.user
    machines = Equipment.objects.select_related("team") if can_review(member) else pages.visible_equipment()
    equipment = get_object_or_404(machines, slug=slug)  # retired ones only for those who review them
    on_team = pages.rules.is_on_team(member, equipment) and not equipment.archived
    team = equipment.team
    sessions = Paginator(pages.recent_sessions(equipment), SESSIONS_PER_PAGE).get_page(request.GET.get("page"))
    context = {
        "equipment": equipment,
        "photos": list(equipment.photos.all()),
        "help_html": to_html(equipment.help_text),
        "ppe": [item.strip() for item in equipment.ppe.split(",") if item.strip()],
        "team_listed": team is not None and getattr(getattr(team, "info", None), "listed", True),
        "state": pages.training_state(member, equipment),
        "refusal": pages.refusal(member, equipment),
        "course_link": moodle.course_link(equipment.moodle_course_id) if equipment.moodle_course_id else "",
        "can_check_course": moodle.enabled(),
        "people": pages.people(equipment),
        "usage": pages.usage_minutes(equipment),
        "sessions": sessions,
        "hours": pages.running_hours(equipment),
        "services": equipment.service_records.select_related("recorded_by")[:20],
        "status_changes": equipment.status_changes.select_related("reported_by")[:10],
        "on_team": on_team,
        "can_report": not equipment.archived and pages.can_report(member, equipment),
        "life": lifetime.lifetime(equipment) if equipment.archived else None,
        "awaiting_reason": pages.awaiting_reason(member, equipment),
        "reclassify_to": pages.RECLASSIFY_TO,
        "today": timezone.localdate().isoformat(),
        "minimum_minutes": pages.rules.minimum_charge_seconds() // 60,
    }
    if pages.can_sign_off(member, equipment):
        context["sign_off"] = pages.waiting_for_sign_off(equipment, member)
    if equipment.requires_induction and not equipment.archived:
        # Links to group email, for the groups about this machine the member may email (06 §6.6).
        allowed = {choice.key for choice in group_email.allowed(member)}
        context["email_groups"] = {
            kind: f"{kind}:{equipment.induction_key}"
            for kind in ("trained", "waiting")
            if f"{kind}:{equipment.induction_key}" in allowed
        }
    return render(request, "equipment/detail.html", context)


@require_GET
def photo(request, slug, photo_id, size):
    """A photo of a machine, for those who may see its page (05 §5.7). `size` is "full" or "thumb"."""
    if (response := _members_only(request)) is not None:
        return response
    machines = Equipment.objects.all() if can_review(request.user) else pages.visible_equipment()
    equipment = get_object_or_404(machines, slug=slug)
    found = get_object_or_404(equipment.photos, pk=photo_id)
    if size not in ("full", "thumb"):
        raise Http404
    file = found.image if size == "full" else found.thumbnail
    response = FileResponse(file.open("rb"), content_type="image/jpeg")
    response["Cache-Control"] = "private, max-age=3600"
    return response


def retired(request):
    """Retired equipment, each with its lifetime figures (05 §5.7)."""
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next={request.path}")
    if not can_review(request.user):
        raise Http404
    lives = [lifetime.lifetime(machine) for machine in lifetime.retired()]
    totals = {
        key: sum(getattr(life, key) for life in lives)
        for key in ("access_fees", "usage_fees", "expenses", "income", "net")
    }
    totals["bought"] = sum(life.machine.purchase_price_pence or 0 for life in lives)
    return render(request, "equipment/retired.html", {"lives": lives, "totals": totals})


def _act(request, slug, action, success):
    if (response := _members_only(request)) is not None:
        return response
    equipment = get_object_or_404(pages.visible_equipment(), slug=slug)
    try:
        action(equipment)
    except pages.PageError as error:
        messages.error(request, str(error))
    else:
        if success:
            messages.success(request, success)
    return redirect("equipment:detail", slug=equipment.slug)


@require_POST
def report(request, slug):
    status = request.POST.get("status", "")
    success = "Thanks: the machine's team has been told." if status == Equipment.Status.OUT_OF_ORDER else "Saved."
    return _act(
        request, slug, lambda e: pages.report_status(e, request.user, status, request.POST.get("note", "")), success
    )


@require_POST
def service(request, slug):
    try:
        serviced_at = date.fromisoformat(request.POST.get("serviced_at", ""))
    except ValueError:
        messages.error(request, "Give the date of the service.")
        return redirect("equipment:detail", slug=slug)
    return _act(
        request,
        slug,
        lambda e: pages.record_service(e, request.user, serviced_at, request.POST.get("note", "")),
        "Service recorded: the hours since the last service start again.",
    )


@require_POST
def reclassify(request, slug, session_id):
    def action(equipment):
        session = get_object_or_404(UsageSession, pk=session_id, equipment=equipment)
        pages.reclassify(session, request.user, request.POST.get("reason", ""))

    return _act(request, slug, action, "Session updated; any fee went back to the member's credit.")


@require_POST
def sign_off(request, slug, induction_id):
    def action(equipment):
        induction = pages.sign_off(equipment, request.user, induction_id)
        messages.success(request, f"{induction.member.display_name} is signed off on {equipment.name}.")

    response = _act(request, slug, action, None)
    response["Location"] += "#sign-off"  # back to the list of members waiting
    return response
