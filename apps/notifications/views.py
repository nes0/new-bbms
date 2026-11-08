"""The member's notifications page (spec 06 §6.5). Members only ever see and change their own."""

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import Notification

PER_PAGE = 25


def _mine(request):
    return Notification.objects.filter(member=request.user)


def _back(request):
    """Back to the same page of the list after a button."""
    url = reverse("notifications:list")
    page = request.POST.get("page", "")
    return redirect(f"{url}?page={page}" if page.isdigit() else url)


@login_required
def notifications(request):
    only_unread = request.GET.get("show") == "unread"
    items = _mine(request)
    if only_unread:
        items = items.filter(unread=True)
    page = Paginator(items, PER_PAGE).get_page(request.GET.get("page"))
    return render(request, "notifications/list.html", {"page": page, "only_unread": only_unread})


@login_required
def open_notification(request, notification_id):
    """Following a notification's link counts as reading it."""
    notification = get_object_or_404(_mine(request), pk=notification_id)
    if notification.unread:
        notification.unread = False
        notification.save(update_fields=["unread"])
    return redirect(notification.url or reverse("notifications:list"))


@login_required
@require_POST
def mark(request, notification_id, state):
    if state not in ("read", "unread"):
        raise Http404
    notification = get_object_or_404(_mine(request), pk=notification_id)
    notification.unread = state == "unread"
    notification.save(update_fields=["unread"])
    return _back(request)


@login_required
@require_POST
def mark_all_read(request):
    _mine(request).filter(unread=True).update(unread=False)
    return _back(request)
