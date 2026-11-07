"""The member's own account page and the changes they can make themselves (spec 03 §3.1, §3.7–3.8)."""

import logging

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import ProtectedError
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.access import tags
from apps.access.decisions import door_refusal_reason
from apps.equipment.models import Equipment
from apps.payments.credit import credit_pence

from . import id_documents
from .account_forms import AddressForm, DetailsForm, LeaveForm, SubscriptionForm
from .models import Member

logger = logging.getLogger(__name__)


def _trained_on(member):
    """The machines a member is signed off on, by name. Machines sharing an induction category all count."""
    categories = {i.category for i in member.inductions.all() if i.is_trained}
    machines = Equipment.objects.filter(archived=False, requires_induction=True)
    return sorted(m.name for m in machines if m.induction_key in categories)


@login_required
def account(request):
    member = request.user
    fobs = [
        {"label": tags.format_card_number(fob.card_number) if fob.card_number is not None else fob.tag_id}
        for fob in member.fobs.filter(active=True)
    ]
    context = {
        "member": member,
        "address": member.current_address,
        "pending_address": member.pending_address,
        "id_status": id_documents.status(member),
        "id_check": member.id_checks.first(),
        "credit_pence": credit_pence(member),
        "fobs": fobs,
        "trained_on": _trained_on(member),
        "door_reason": door_refusal_reason(member),  # "" when their fob opens the front door (05 §5.2)
    }
    return render(request, "members/account.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def edit_details(request):
    form = (
        DetailsForm(request.POST, instance=request.user)
        if request.method == "POST"
        else DetailsForm(instance=request.user)
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Your details have been updated.")
        return redirect("members:account")
    return render(request, "members/edit_details.html", {"form": form})


@login_required
@require_http_methods(["GET", "POST"])
def change_address(request):
    member = request.user
    if request.method == "POST":
        form = AddressForm(request.POST)
        if form.is_valid():
            form.save_for(member)
            messages.success(request, "Thanks. A trustee will check your new address; until then we keep the old one.")
            return redirect("members:account")
    else:
        # Start from the latest address given, so a small correction is easy.
        latest = member.addresses.first()
        form = AddressForm(instance=None, initial=_address_initial(latest))
    return render(request, "members/change_address.html", {"form": form, "address": member.current_address})


def _address_initial(address):
    if address is None:
        return {}
    return {name: getattr(address, name) for name in ("line_1", "line_2", "line_3", "line_4", "postcode")}


@login_required
@require_http_methods(["GET", "POST"])
def change_subscription(request):
    member = request.user
    current = (member.monthly_subscription_pence or 0) // 100 or None
    if request.method == "POST":
        form = SubscriptionForm(request.POST)
        if form.is_valid():
            member.monthly_subscription_pence = form.cleaned_data["monthly_subscription"] * 100
            member.save(update_fields=["monthly_subscription_pence"])
            # TODO(subscriptions app): only for Direct Debit and credit payers (03 §3.7); applies from the next
            # charge, and a charge already created keeps its amount.
            messages.success(request, f"Your monthly subscription is now £{form.cleaned_data['monthly_subscription']}.")
            return redirect("members:account")
    else:
        form = SubscriptionForm(initial={"monthly_subscription": current})
    return render(request, "members/change_subscription.html", {"form": form, "current": current})


@login_required
@require_http_methods(["GET", "POST"])
def leave(request):
    """Leaving (03 §3.8). For now only an account still setting up can do it here: it is deleted outright."""
    member = request.user
    can_delete = member.status == Member.Status.SETTING_UP
    form = LeaveForm(request.POST) if request.method == "POST" else LeaveForm()
    if request.method == "POST" and can_delete and form.is_valid():
        member_id = member.pk
        try:
            with transaction.atomic():
                member.delete()  # also deletes their addresses, proof-of-identity uploads and ID checks
        except ProtectedError:
            # Something that must be kept refers to them (e.g. a payment or a fob): a trustee has to sort it out.
            messages.error(request, "We couldn't delete your account automatically. Please contact the trustees.")
            return redirect("members:account")
        logout(request)
        logger.info("Setting-up account %s deleted by its owner", member_id)
        messages.success(request, "Your account has been deleted.")
        return redirect("home")
    # TODO(subscriptions app): for other members, "leave" cancels the Direct Debit and sets status = leaving.
    return render(request, "members/leave.html", {"form": form, "can_delete": can_delete})
