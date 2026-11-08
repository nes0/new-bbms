"""The member's payment pages (spec 03 §3.1, §3.7-3.8) and the fake GoCardless page used when developing."""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from apps.members.models import Member
from apps.payments.credit import credit_pence
from apps.payments.services import InsufficientCredit

from . import billing, gocardless, membership
from .models import Charge, MandateSetup
from .webhooks import finish_setup

logger = logging.getLogger(__name__)

SETUP_SESSION_KEY = "billing_request_id"
# Paying from credit as the monthly method is offered to members without one (03 §3.1).
CREDIT_SETUP_STATUSES = (Member.Status.SETTING_UP, Member.Status.LEAVING)


def can_set_up_credit(member):
    return not member.payment_method and member.status in CREDIT_SETUP_STATUSES


def can_set_up_direct_debit(member):
    return member.status != Member.Status.LEFT and not member.banned


@login_required
def payment(request):
    member = request.user
    charges = member.charges.select_related("rental__locker").order_by("-charge_date", "-pk")[:24]
    context = {
        "member": member,
        "credit_pence": credit_pence(member),
        "charges": charges,
        "unpaid": [c for c in charges if c.status in Charge.UNPAID],
        "can_set_up_credit": can_set_up_credit(member),
        "can_set_up_direct_debit": can_set_up_direct_debit(member),
        "can_switch": member.payment_method in (Member.PaymentMethod.DIRECT_DEBIT, Member.PaymentMethod.CREDIT)
        and member.status != Member.Status.LEFT,
    }
    return render(request, "billing/payment.html", context)


@login_required
@require_POST
def direct_debit_start(request):
    member = request.user
    if not can_set_up_direct_debit(member):
        messages.error(request, "Rejoin first, then set up your Direct Debit.")
        return redirect("billing:payment")
    try:
        request_id, url = gocardless.client().start_mandate_setup(
            member,
            redirect_uri=request.build_absolute_uri(reverse("billing:direct-debit-done")),
            exit_uri=request.build_absolute_uri(reverse("billing:payment")),
        )
    except gocardless.GoCardlessError:
        messages.error(request, "We couldn't reach GoCardless just now. Please try again in a few minutes.")
        return redirect("billing:payment")
    MandateSetup.objects.create(member=member, billing_request_id=request_id)
    request.session[SETUP_SESSION_KEY] = request_id
    return redirect(url)


@login_required
def direct_debit_done(request):
    """GoCardless sends the member back here once they've authorised the Direct Debit."""
    request_id = request.session.pop(SETUP_SESSION_KEY, None)
    setup = MandateSetup.objects.filter(member=request.user, billing_request_id=request_id).first()
    if setup is None:
        setup = MandateSetup.objects.filter(member=request.user, completed_at__isnull=True).first()
    if setup is None:
        return redirect("billing:payment")
    try:
        done = finish_setup(setup)
    except gocardless.GoCardlessError:
        done = False
    if done:
        messages.success(request, "Your Direct Debit is set up. Thank you!")
        return redirect("members:account")
    # GoCardless also tells BBMS by webhook, so this sorts itself out shortly.
    messages.info(request, "GoCardless hasn't confirmed your Direct Debit yet. It can take a minute; check back soon.")
    return redirect("billing:payment")


@login_required
@require_POST
def credit_start(request):
    """Pay the subscription from member credit (03 §3.1)."""
    member = request.user
    if not can_set_up_credit(member):
        messages.error(request, "Paying from credit isn't available for your account. Please ask a trustee.")
        return redirect("billing:payment")
    member.payment_method = Member.PaymentMethod.CREDIT
    member.payment_day = membership.payment_day_for(timezone.localdate())
    member.save(update_fields=["payment_method", "payment_day"])
    membership.activate(member)
    messages.success(
        request,
        "You'll pay from your credit. Your subscription is taken from it on the "
        f"{member.payment_day}{_ordinal(member.payment_day)} of each month: keep it topped up.",
    )
    return redirect("members:account")


@login_required
@require_POST
def change_method(request):
    """Direct Debit ⇄ credit (03 §3.7). Switching to credit keeps the Direct Debit as a backup."""
    member = request.user
    wanted = request.POST.get("method")
    if member.payment_method not in (Member.PaymentMethod.DIRECT_DEBIT, Member.PaymentMethod.CREDIT):
        messages.error(request, "Please ask a trustee to change how you pay.")
    elif wanted == Member.PaymentMethod.DIRECT_DEBIT and not member.mandate_id:
        messages.error(request, "Set up a Direct Debit first.")
    elif wanted in (Member.PaymentMethod.DIRECT_DEBIT, Member.PaymentMethod.CREDIT):
        member.payment_method = wanted
        member.save(update_fields=["payment_method"])
        label = Member.PaymentMethod(wanted).label
        messages.success(request, f"You now pay by {label.lower()}.")
    return redirect("billing:payment")


@login_required
@require_POST
def pay_charge(request, charge_id):
    charge = get_object_or_404(Charge, pk=charge_id, member=request.user)
    try:
        billing.pay_charge_from_credit(charge, timezone.localdate())
    except InsufficientCredit as error:
        messages.error(request, f"{error} Top up your credit first.")
    except ValueError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Paid from your credit. Thank you!")
    return redirect("billing:payment")


@login_required
@require_POST
def rejoin(request):
    member = membership.rejoin(request.user)
    if member.status == Member.Status.SETTING_UP:
        messages.success(request, "Welcome back! Set up your monthly payment to become active again.")
    return redirect("members:account")


def _ordinal(day):
    return "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


# --- the fake GoCardless page (local development only) ---


@require_http_methods(["GET", "POST"])
def fake_gocardless(request, request_id):
    """Stands in for GoCardless's hosted page when there's no access token. DEBUG only."""
    if not settings.DEBUG or settings.GOCARDLESS_ACCESS_TOKEN:
        raise Http404
    fake = gocardless.fake()
    if request_id not in fake.billing_requests:
        raise Http404
    if request.method == "POST":
        return redirect(fake.authorise(request_id))
    return render(
        request, "billing/fake_gocardless.html", {"request_id": request_id, **fake.billing_requests[request_id]}
    )


@require_http_methods(["GET", "POST"])
def fake_gocardless_payments(request):
    """Play GoCardless when developing: send "confirmed", "failed" and other events for the fake's payments."""
    if not settings.DEBUG or settings.GOCARDLESS_ACCESS_TOKEN:
        raise Http404
    from .webhooks import process_pending, store_events

    fake = gocardless.fake()
    if request.method == "POST":
        payment_id, action = request.POST.get("payment"), request.POST.get("action")
        details = {"cause": request.POST.get("cause", "")} if action == "failed" else {}
        if payment_id in fake.payments and action in ("submitted", "confirmed", "failed", "cancelled"):
            fake.set_status(payment_id, action)
            event_id = f"EVFAKE{timezone.now().timestamp():.6f}".replace(".", "")
            store_events(
                [
                    {
                        "id": event_id,
                        "created_at": timezone.now().isoformat(),
                        "resource_type": "payments",
                        "action": action,
                        "links": {"payment": payment_id},
                        "details": details,
                    }
                ]
            )
            process_pending()
            messages.success(request, f"Sent payments.{action} for {payment_id}.")
        return redirect("billing:fake-gocardless-payments")
    return render(request, "billing/fake_gocardless_payments.html", {"payments": fake.payments})
