"""The member's credit page (spec 04 §4.7), and paying for things by card, Direct Debit, PayPal or from credit
(04 §4.1-4.4).
"""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from apps.members.decorators import email_confirmed_required

from . import checkout, paypal, paypal_ipn
from .cards import CardError
from .credit import credit_movements, credit_pence
from .forms import TopUpForm, WithdrawalRequestForm
from .models import CardCheckout, WithdrawalRequest
from .services import InsufficientCredit

logger = logging.getLogger(__name__)


@login_required
def credit(request):
    member = request.user
    form = WithdrawalRequestForm(request.POST or None, member=member)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Thanks: the trustees will pay it to your bank account and let you know.")
        return redirect("payments:credit")

    page = Paginator(credit_movements(member).select_related("usage_session__equipment"), 25).get_page(
        request.GET.get("page")
    )
    low, high = checkout.top_up_limits()
    context = {
        "balance": credit_pence(member),
        "page": page,
        "form": form,
        "withdrawals": WithdrawalRequest.objects.filter(member=member)[:5],
        "top_up_low": low,
        "top_up_high": high,
        "back": "credit",
        "has_direct_debit": bool(member.mandate_id),
        "paypal": paypal.available(),
    }
    return render(request, "payments/credit.html", context)


# --- paying for things: by card (Stripe) or from credit (04 §4.1, §4.3) ---

# Pages a form may ask to return to (named, never a URL from the browser).
BACK = {"credit": "payments:credit", "payment": "billing:payment"}
# Where to send the member once a payment is done, by what it was for.
AFTER = {CardCheckout.Purpose.TOP_UP: "payments:credit", CardCheckout.Purpose.CHARGE: "billing:payment"}


@login_required
@email_confirmed_required
def pay(request):
    member = request.user
    low, high = checkout.top_up_limits()
    context = {
        "items": checkout.payables(member),
        "balance": credit_pence(member),
        "top_up_form": TopUpForm(),
        "top_up_low": low,
        "top_up_high": high,
        "has_direct_debit": bool(member.mandate_id),
        "paypal": paypal.available(),
    }
    return render(request, "payments/pay.html", context)


@login_required
@require_POST
@email_confirmed_required
def pay_by_card(request):
    member = request.user
    key = request.POST.get("item", "")
    if key == "top-up":
        form = TopUpForm(request.POST)
        if not form.is_valid():
            messages.error(request, " ".join(form.errors["amount"]))
            return redirect(BACK.get(request.POST.get("back"), "payments:pay"))
        purpose, amount, label, extra = CardCheckout.Purpose.TOP_UP, form.cleaned_data["amount"], "Credit top-up", {}
    else:
        item = checkout.find_payable(member, key)
        if item is None:
            messages.error(request, "That doesn't need paying any more.")
            return redirect("payments:pay")
        purpose, amount, label = item.purpose, item.amount_pence, item.label
        extra = {"equipment": item.equipment, "charge": item.charge}
    try:
        url = checkout.start_card_checkout(
            member,
            purpose=purpose,
            amount_pence=amount,
            label=label,
            success_url=request.build_absolute_uri(reverse("payments:card-done")),
            cancel_url=request.build_absolute_uri(reverse("payments:card-cancelled")),
            **extra,
        )
    except CardError as error:
        messages.error(request, str(error))
        return redirect("payments:pay")
    return redirect(url)


@login_required
@require_POST
@email_confirmed_required
def pay_with_direct_debit(request):
    """A one-off Direct Debit from the member's mandate (04 §4.2)."""
    from apps.billing.gocardless import GoCardlessError

    member = request.user
    key = request.POST.get("item", "")
    if key == "top-up":
        form = TopUpForm(request.POST)
        if not form.is_valid():
            messages.error(request, " ".join(form.errors["amount"]))
            return redirect(BACK.get(request.POST.get("back"), "payments:pay"))
        amount = form.cleaned_data["amount"]
        item = checkout.Payable(CardCheckout.Purpose.TOP_UP, "Credit top-up", amount, f"top-up-{amount}")
    else:
        item = checkout.find_payable(member, key)
        if item is None:
            messages.error(request, "That doesn't need paying any more.")
            return redirect("payments:pay")
    try:
        checkout.pay_by_direct_debit(
            member,
            purpose=item.purpose,
            amount_pence=item.amount_pence,
            label=item.label,
            key=item.key,
            equipment=item.equipment,
            charge=item.charge,
        )
    except checkout.DirectDebitUnavailable as error:
        messages.error(request, str(error))
        return redirect("payments:pay")
    except GoCardlessError:
        messages.error(request, "We couldn't reach GoCardless just now. Please try again in a few minutes.")
        return redirect("payments:pay")
    messages.success(
        request,
        f"{item.label}: we've asked for it by Direct Debit. It leaves your bank account in a few working days.",
    )
    return redirect(AFTER.get(item.purpose, "payments:pay"))


@login_required
@require_POST
@email_confirmed_required
def pay_with_credit(request):
    item = checkout.find_payable(request.user, request.POST.get("item", ""))
    if item is None:
        messages.error(request, "That doesn't need paying any more.")
        return redirect("payments:pay")
    try:
        checkout.pay_from_credit(request.user, item)
    except InsufficientCredit as error:
        messages.error(request, f"{error} Top up your credit first.")
        return redirect("payments:pay")
    messages.success(request, f"{item.label}: paid from your credit. Thank you!")
    return redirect(AFTER.get(item.purpose, "payments:pay"))


@login_required
def card_done(request):
    """Stripe sends the member back here after paying."""
    found = CardCheckout.objects.filter(member=request.user, session_id=request.GET.get("session_id", "")).first()
    if found is None:
        return redirect("payments:pay")
    try:
        payment = checkout.check_with_stripe(found)
    except CardError:
        payment = None
    if payment is not None:
        found.refresh_from_db()
        messages.success(request, found.note or f"{found.get_purpose_display()}: paid by card. Thank you!")
    else:
        # Stripe also tells BBMS by webhook, so this sorts itself out shortly.
        messages.info(request, "Stripe hasn't confirmed your payment yet. It can take a minute; check back soon.")
    return redirect(AFTER.get(found.purpose, "payments:pay"))


@login_required
def card_cancelled(request):
    messages.info(request, "Payment cancelled: nothing was taken.")
    return redirect("payments:pay")


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Stripe calls this (04 §4.3). The signature is checked with the endpoint's signing secret."""
    import stripe

    secret = settings.STRIPE_WEBHOOK_SECRET
    if not secret:
        return HttpResponse(status=403)
    try:
        event = stripe.Webhook.construct_event(request.body, request.headers.get("Stripe-Signature", ""), secret)
    except stripe.SignatureVerificationError:
        logger.warning("Stripe webhook with a bad signature refused")
        return HttpResponse(status=403)
    except ValueError:
        return HttpResponse(status=400)
    # Stripe's objects aren't dicts (no .get()): use attributes.
    object_id = getattr(event.data.object, "id", "") or ""
    try:
        checkout.handle_event(event.id, event.type, object_id)
    except Exception:
        return HttpResponse(status=500)  # Stripe retries; the error is kept on the event in the admin
    return HttpResponse(status=200)


@require_http_methods(["GET", "POST"])
def fake_stripe(request, session_id):
    """Stands in for Stripe's Checkout page when there's no key. DEBUG only."""
    from . import cards

    if not settings.DEBUG or settings.STRIPE_SECRET_KEY:
        raise Http404
    fake = cards.fake()
    session = fake.sessions.get(session_id)
    if session is None:
        raise Http404
    if request.method == "POST":
        return redirect(fake.pay(session_id))
    return render(request, "payments/fake_stripe.html", {"session_id": session_id, "session": session})


# --- PayPal (04 §4.4) ---


def paypal_urls(request, purpose):
    """Where PayPal sends its notifications, and the member when they've paid or given up."""
    return {
        "notify_url": request.build_absolute_uri(reverse("payments:paypal-ipn")),
        "return_url": request.build_absolute_uri(reverse("payments:paypal-done")) + f"?for={purpose}",
        "cancel_url": request.build_absolute_uri(reverse("payments:paypal-cancelled")),
    }


@login_required
@require_POST
@email_confirmed_required
def pay_by_paypal(request):
    """A credit top-up by PayPal. PayPal only says what it took once the member has paid, and that's what's added."""
    back = BACK.get(request.POST.get("back"), "payments:pay")
    if not paypal.available():
        messages.error(request, "PayPal payments aren't available on this site at the moment.")
        return redirect(back)
    form = TopUpForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(form.errors["amount"]))
        return redirect(back)
    return redirect(paypal.top_up_url(request.user, form.cleaned_data["amount"], **paypal_urls(request, "top-up")))


@login_required
def paypal_done(request):
    """PayPal sends the member back here. The payment counts once PayPal's notification arrives."""
    if request.GET.get("for") == "subscription":
        messages.success(
            request,
            "Thank you! Your PayPal subscription is set up. Your membership updates as soon as PayPal tells us, "
            "usually within a few minutes.",
        )
        return redirect("billing:payment")
    messages.success(
        request, "Thank you! Your credit goes up as soon as PayPal tells us about the payment, usually within minutes."
    )
    return redirect("payments:credit")


@login_required
def paypal_cancelled(request):
    messages.info(request, "PayPal payment cancelled: nothing was taken.")
    return redirect("payments:pay")


@csrf_exempt
@require_POST
def paypal_notification(request):
    """PayPal's IPN (04 §4.4). Checked with PayPal before anything is stored."""
    if not paypal.available():
        return HttpResponse(status=403)
    raw = request.body
    try:
        genuine = paypal.verify(raw)
    except paypal.Unreachable:
        return HttpResponse(status=503)  # PayPal sends it again later
    if not genuine:
        logger.warning("PayPal notification refused: PayPal didn't confirm it")
        return HttpResponse(status=400)
    if paypal_ipn.store(paypal.parse(raw)):
        paypal_ipn.process_pending()
    return HttpResponse(status=200)


# --- the fake PayPal (local development only) ---


def _fake_only():
    if not settings.DEBUG or not paypal.using_fake():
        raise Http404


def _fake_send(data):
    """As if PayPal had sent BBMS a notification."""
    from django.utils import timezone

    stamp = f"{timezone.now().timestamp():.6f}".replace(".", "")
    data = {"receiver_email": paypal.FAKE_ACCOUNT, "mc_currency": "GBP", "ipn_track_id": f"fake{stamp}", **data}
    paypal_ipn.store(data)
    paypal_ipn.process_pending()


def _fake_txn():
    from django.utils import timezone

    return "FAKE" + f"{timezone.now().timestamp():.6f}".replace(".", "")[-13:]


@require_http_methods(["GET", "POST"])
def fake_paypal(request):
    """Stands in for PayPal's payment page when there's no PayPal account set. DEBUG only."""
    _fake_only()
    params = request.GET
    member_id = paypal.member_id_from(params.get("custom", ""))
    subscription = params.get("cmd") == "_xclick-subscriptions"
    amount = params.get("a3") if subscription else params.get("amount")
    if member_id is None or not amount:
        raise Http404
    if request.method == "POST":
        from apps.members.models import Member

        member = Member.objects.get(pk=member_id)
        payer = {"payer_email": member.paypal_email or member.email, "custom": params["custom"]}
        if subscription:
            subscr_id = "I-" + _fake_txn()
            _fake_send({"txn_type": "subscr_signup", "subscr_id": subscr_id, "amount3": amount, **payer})
            _fake_send(
                {
                    "txn_type": "subscr_payment",
                    "subscr_id": subscr_id,
                    "txn_id": _fake_txn(),
                    "payment_status": "Completed",
                    "mc_gross": amount,
                    "mc_fee": _fake_fee(amount),
                    **payer,
                }
            )
        else:
            _fake_send(
                {
                    "txn_type": "web_accept",
                    "txn_id": _fake_txn(),
                    "payment_status": "Completed",
                    "mc_gross": amount,
                    "mc_fee": _fake_fee(amount),
                    **payer,
                }
            )
        return redirect(reverse("payments:paypal-done") + ("?for=subscription" if subscription else ""))
    context = {"item": params.get("item_name", ""), "amount": amount, "subscription": subscription}
    return render(request, "payments/fake_paypal.html", context)


def _fake_fee(amount):
    from decimal import Decimal

    return f"{Decimal(amount) * Decimal('0.029') + Decimal('0.30'):.2f}"  # a typical PayPal fee


@require_http_methods(["GET", "POST"])
def fake_paypal_tools(request):
    """Play PayPal when developing: the next monthly payment, a failed one, a cancellation or a refund."""
    from .models import Payment, PayPalNotification

    _fake_only()
    signups = PayPalNotification.objects.filter(txn_type="subscr_signup", subscr_id__startswith="I-FAKE")
    payments = Payment.objects.filter(source=Payment.Source.PAYPAL, corrects__isnull=True, amount_pence__gt=0)
    if request.method == "POST":
        action = request.POST.get("action")
        signup = signups.filter(subscr_id=request.POST.get("subscr_id", "")).first()
        if signup is not None and action in ("payment", "failed", "cancel"):
            base = {
                "subscr_id": signup.subscr_id,
                "payer_email": signup.payer_email,
                "custom": signup.data.get("custom", ""),
            }
            amount = signup.data.get("amount3", "0")
            if action == "payment":
                _fake_send(
                    {
                        "txn_type": "subscr_payment",
                        "txn_id": _fake_txn(),
                        "payment_status": "Completed",
                        "mc_gross": amount,
                        "mc_fee": _fake_fee(amount),
                        **base,
                    }
                )
            elif action == "failed":
                _fake_send({"txn_type": "subscr_failed", **base})
            else:
                _fake_send({"txn_type": "subscr_cancel", **base})
            messages.success(request, f"Sent {action} for {signup.subscr_id}.")
        payment = payments.filter(pk=request.POST.get("payment") or 0).first()
        if payment is not None and action == "refund":
            _fake_send(
                {
                    "txn_id": _fake_txn(),
                    "parent_txn_id": payment.source_id,
                    "payment_status": "Refunded",
                    "mc_gross": f"-{payment.amount_pence / 100:.2f}",
                }
            )
            messages.success(request, f"Refunded payment {payment.pk}.")
        return redirect("payments:fake-paypal-tools")
    context = {"signups": signups.order_by("-pk"), "payments": payments.select_related("member")[:30]}
    return render(request, "payments/fake_paypal_tools.html", context)
