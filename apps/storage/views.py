"""The members' lockers page, and renting and giving up a locker there (spec 06 §6.2, "Renting", "Pages")."""

from itertools import groupby

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.billing.billing import MINIMUM_FIRST_RENT_PENCE, rent_payment_problem
from apps.members.decorators import email_confirmed_required
from apps.payments.credit import credit_pence

from . import rules
from .models import Locker, LockerRental


@login_required
def lockers(request):
    member = request.user
    available = sorted(
        rules.available_lockers(),
        key=lambda item: (item[0].location.sort_order, item[0].location.name, item[0].size.sort_order, item[0].number),
    )
    # Location → size → [(locker, monthly rent)], for the page.
    by_location = [
        (location, [(size, list(items)) for size, items in groupby(group, key=lambda item: item[0].size)])
        for location, group in groupby(available, key=lambda item: item[0].location)
    ]
    mine = [
        (rental, rules.rental_rent_pence(rental), rules.upcoming_rent(rental)) for rental in rules.open_rentals(member)
    ]
    context = {
        "by_location": by_location,
        "mine": mine,
        "limit": rules.locker_limit(member),
        "problem": rules.self_service_problem(member),
        "set_up_payment": rent_payment_problem(member) is not None,
    }
    return render(request, "storage/lockers.html", context)


@login_required
@email_confirmed_required
@require_http_methods(["GET", "POST"])
def rent(request, locker_id):
    """Check what renting costs, then rent it: the first rent is taken straight away."""
    member = request.user
    locker = get_object_or_404(Locker.objects.select_related("location", "size"), pk=locker_id, retired=False)
    if request.method == "POST":
        try:
            rental, amount = rules.rent_locker(locker, member)
        except rules.LockerError as error:
            messages.error(request, str(error))
        else:
            paid = f" We've taken {rules.pounds(amount)} for the days until your payment day." if amount else ""
            messages.success(request, f"{rental.locker} is yours.{paid}")
            return redirect("storage:lockers")
    monthly = rules.advertised_rent_pence(locker)
    problem = rules.self_service_problem(member, locker)
    first = next_day = None
    if monthly is not None:
        first, next_day = rules.first_rent(member, monthly, timezone.localdate())
        if first < MINIMUM_FIRST_RENT_PENCE:
            first = 0
    by_credit = member.payment_method == member.PaymentMethod.CREDIT
    credit = credit_pence(member)
    top_up = not problem and by_credit and not member.has_direct_debit and first and credit < first
    if top_up:
        problem = f"The first rent is {rules.pounds(first)} and you have {rules.pounds(credit)} of credit."
    context = {
        "locker": locker,
        "monthly": monthly,
        "first": first,
        "next_day": next_day,
        "problem": problem,
        "by_credit": by_credit,
        "credit": credit,
        "has_direct_debit": member.has_direct_debit,
        "top_up": top_up,
        "set_up_payment": rent_payment_problem(member) is not None,
    }
    return render(request, "storage/rent.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def give_up(request, rental_id):
    rental = get_object_or_404(
        LockerRental.objects.select_related("locker__location"), pk=rental_id, member=request.user, ended_at=None
    )
    if request.method == "POST":
        try:
            rules.give_up(rental, request.user)
        except rules.LockerError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, f"You've given up {rental.locker}. Please leave it empty and unlocked.")
        return redirect("storage:lockers")
    return render(request, "storage/give_up.html", {"rental": rental})
