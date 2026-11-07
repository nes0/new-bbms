"""The member's own credit page (spec 04 §4.7)."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import redirect, render

from .credit import credit_movements, credit_pence
from .forms import WithdrawalRequestForm
from .models import WithdrawalRequest


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
    context = {
        "balance": credit_pence(member),
        "page": page,
        "form": form,
        "withdrawals": WithdrawalRequest.objects.filter(member=member)[:5],
    }
    return render(request, "payments/credit.html", context)
