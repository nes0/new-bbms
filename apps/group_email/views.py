"""
The group email page (spec 06 §6.6): write an email, send yourself a test, check who it goes to, then send it.

Only members whose rules let them email a group see the page (admins see every group); the groups they may choose
come from `audiences.allowed`, and the choice is checked again when it's sent.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from . import audiences, sending
from .forms import GroupEmailForm
from .models import GroupEmail


@login_required
@require_http_methods(["GET", "POST"])
def compose(request):
    groups = audiences.allowed(request.user)
    if not groups:
        raise Http404
    action = request.POST.get("action", "")
    if request.method == "POST":
        form = GroupEmailForm(request.POST, groups=groups)
    else:
        form = GroupEmailForm(initial={"to": request.GET.get("to", "")}, groups=groups)
    context = {"form": form, "sent": GroupEmail.objects.filter(sent_by=request.user)[:20]}
    if request.method == "POST" and form.is_valid():
        key, subject, body = (form.cleaned_data[name] for name in ("to", "subject", "body"))
        if action == "test":
            if sending.send_test(request.user, key, subject, body):
                messages.success(request, f"A test has been sent to you at {request.user.email}.")
            else:
                messages.error(request, "The test couldn't be sent. Please try again in a few minutes.")
        elif action == "send" and request.POST.get("checked_to") == key:
            record = sending.send(request.user, key, subject, body)
            if record.failed:
                messages.error(
                    request,
                    f"Sent to {record.recipients - record.failed} of {record.recipients} members: the mail server "
                    f"didn't take the rest. Please tell an admin.",
                )
            else:
                messages.success(request, f"Sent to {record.recipients} members ({record.audience_label}).")
            return redirect("group_email:compose")
        else:
            # "Check", or a send after the group was changed: show who it will go to before sending.
            context["check"] = {
                "to": key,
                "count": audiences.members_of(key).count(),
                "label": audiences.label(key),
            }
    return render(request, "group_email/compose.html", context)
