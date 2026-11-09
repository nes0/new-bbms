"""The member's own account page and the changes they can make themselves (spec 03 §3.1, §3.7–3.8)."""

import logging

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import transaction
from django.db.models import ProtectedError
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from apps.access import attendance, tags
from apps.access.decisions import door_refusal_reason
from apps.core import policies
from apps.discord import client as discord
from apps.equipment.models import Equipment
from apps.payments.credit import credit_pence
from apps.proposals.voting import proposals_waiting_for

from . import email_confirmation, id_documents, induction
from .account_forms import (
    AddressForm,
    DetailsForm,
    EmailChangeForm,
    InductionForm,
    LeaveForm,
    LeaveMembershipForm,
    SubscriptionForm,
)
from .models import Member

logger = logging.getLogger(__name__)


def _ordinal(day):
    return "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _trained_on(member):
    """The machines a member is signed off on, sorted by name. Machines sharing an induction category all count."""
    categories = {i.category for i in member.inductions.all() if i.is_trained}
    machines = Equipment.objects.filter(archived=False, requires_induction=True)
    return sorted((m for m in machines if m.induction_key in categories), key=lambda m: m.name)


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
        "attendance": attendance.attendance(member),  # 05 §5.10
        "door_reason": door_refusal_reason(member),  # "" when their fob opens the front door (05 §5.2)
        "unpaid_charges": member.charges.filter(status__in=("due", "failed")).count(),
        "payment_day_suffix": _ordinal(member.payment_day) if member.payment_day else "",
        "discord_enabled": discord.enabled(),
        "proposals_to_vote_on": proposals_waiting_for(member),
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
def general_induction(request):
    """The general induction (05 §5.9): read the member agreement and the rules, and agree to them."""
    member = request.user
    form = InductionForm(request.POST) if request.method == "POST" else InductionForm()
    if request.method == "POST" and member.induction_completed:
        return redirect("members:induction")
    if request.method == "POST" and form.is_valid():
        induction.complete(member)
        messages.success(request, "Thank you. Someone from the space will confirm your induction when you visit.")
        return redirect("members:account")
    context = {
        "form": form,
        "member": member,
        "agreement": policies.html("member-agreement", top_heading=3),
        "rules": policies.html("rules"),
    }
    return render(request, "members/induction.html", context)


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


# Members choose their own amount only when it's billed by BBMS (03 §3.7); others ask a trustee.
SELF_SERVICE_METHODS = ("", Member.PaymentMethod.DIRECT_DEBIT, Member.PaymentMethod.CREDIT)


@login_required
@require_http_methods(["GET", "POST"])
def change_subscription(request):
    member = request.user
    current = (member.monthly_subscription_pence or 0) // 100 or None
    if member.payment_method not in SELF_SERVICE_METHODS:
        messages.info(request, f"You pay by {member.get_payment_method_display().lower()}: ask a trustee to change it.")
        return redirect("members:account")
    if request.method == "POST":
        form = SubscriptionForm(request.POST)
        if form.is_valid():
            member.monthly_subscription_pence = form.cleaned_data["monthly_subscription"] * 100
            member.save(update_fields=["monthly_subscription_pence"])
            # Charges take the amount when they're billed, so this applies from the next one not yet billed.
            messages.success(request, f"Your monthly subscription is now £{form.cleaned_data['monthly_subscription']}.")
            return redirect("members:account")
    else:
        form = SubscriptionForm(initial={"monthly_subscription": current})
    return render(request, "members/change_subscription.html", {"form": form, "current": current})


@login_required
@require_http_methods(["GET", "POST"])
def leave(request):
    """Leaving (03 §3.8). An account still setting up is deleted outright; anyone else stops paying and leaves when
    their paid-up time runs out.
    """
    member = request.user
    can_delete = member.status == Member.Status.SETTING_UP
    can_leave = member.status in (Member.Status.ACTIVE, Member.Status.SUSPENDED, Member.Status.HONORARY)
    if can_leave:
        return _leave_membership(request, member)
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
    return render(request, "members/leave.html", {"form": form, "can_delete": can_delete})


def _leave_membership(request, member):
    from apps.billing import gocardless, membership

    form = LeaveMembershipForm(request.POST) if request.method == "POST" else LeaveMembershipForm()
    if request.method == "POST" and form.is_valid():
        try:
            membership.start_leaving(member)
        except gocardless.GoCardlessError:
            messages.error(request, "We couldn't reach GoCardless to stop your Direct Debit. Please try again later.")
            return redirect("members:leave")
        messages.success(request, "You're leaving. We've stopped your monthly payments.")
        return redirect("members:account")
    has_lockers = member.locker_rentals.filter(ended_at__isnull=True).exists()
    return render(
        request, "members/leave_membership.html", {"form": form, "member": member, "has_lockers": has_lockers}
    )


# --- Email address: confirming it, and changing it (03 §3.1) ---

EMAIL_PAUSE_SECONDS = 60  # between emails asked for by one member: courtesy to the mail server, and to their inbox


def confirm_email(request, token):
    """The link in the confirmation email. Works without logging in: the link itself is the proof."""
    outcome, member = email_confirmation.confirm(token)
    status = 400 if outcome in (email_confirmation.INVALID, email_confirmation.TAKEN) else 200
    return render(request, "members/confirm_email.html", {"outcome": outcome, "confirmed": member}, status=status)


@login_required
@require_POST
def resend_confirmation(request):
    member = request.user
    if member.email_verified:
        messages.info(request, "Your email address is already confirmed.")
    elif not cache.add(f"email-confirmation:{member.pk}", 1, EMAIL_PAUSE_SECONDS):
        messages.info(request, "We've only just sent one. Please wait a minute, and check your spam folder.")
    else:
        email_confirmation.send_confirmation(member)
        messages.success(request, "We've emailed you a link to confirm your address.")
    return redirect("members:account")


@login_required
@require_http_methods(["GET", "POST"])
def change_email(request):
    member = request.user
    form = EmailChangeForm(member, request.POST) if request.method == "POST" else EmailChangeForm(member)
    if request.method == "POST" and form.is_valid():
        if cache.add(f"email-change:{member.pk}", 1, EMAIL_PAUSE_SECONDS):
            email_confirmation.send_change_request(member, form.cleaned_data["new_email"])
            messages.success(
                request,
                "We've sent a link to your new address. Your login changes when you follow it; until then, keep "
                "using the old one.",
            )
            return redirect("members:account")
        messages.info(request, "We've only just sent one. Please wait a minute and try again.")
    return render(request, "members/change_email.html", {"form": form})
