"""
The expenses pages (spec 06 §6.4): members claim and follow their own claims; those who handle expenses see
everyone's, filter and sort them, and approve or decline them.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Sum
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from apps.members.models import Member
from apps.profiles.views import is_member

from . import claims
from .forms import ExpenseForm
from .models import Expense

PER_PAGE = 25
SORTS = {
    "newest": ("-submitted_at", "-pk"),
    "oldest": ("submitted_at", "pk"),
    "bought": ("-spent_on", "-pk"),
    "biggest": ("-amount_pence", "-pk"),
}


def _expense_for(request, expense_id) -> Expense:
    expense = get_object_or_404(Expense.objects.select_related("member", "decided_by"), pk=expense_id)
    if not claims.can_see(request.user, expense):
        raise Http404
    return expense


@login_required
def index(request):
    see_all = claims.can_see_all(request.user)
    if not see_all and not is_member(request.user) and not request.user.expenses.exists():
        raise Http404
    expenses = Expense.objects.select_related("member")
    filters = {"status": request.GET.get("status", ""), "member": request.GET.get("member", "")}
    if see_all:
        if filters["member"].isdigit():
            expenses = expenses.filter(member_id=int(filters["member"]))
    else:
        expenses = expenses.filter(member=request.user)
    if filters["status"] in Expense.Status.values:
        expenses = expenses.filter(status=filters["status"])
    sort = request.GET.get("sort", "newest")
    expenses = expenses.order_by(*SORTS.get(sort, SORTS["newest"]))
    page = Paginator(expenses, PER_PAGE).get_page(request.GET.get("page"))
    context = {
        "page": page,
        "see_all": see_all,
        "can_claim": is_member(request.user),
        "filters": filters,
        "sort": sort,
        "sorts": [
            ("newest", "Newest claims"),
            ("oldest", "Oldest claims"),
            ("bought", "Date of purchase"),
            ("biggest", "Biggest"),
        ],
        "statuses": Expense.Status.choices,
        "claimants": Member.objects.filter(expenses__isnull=False).distinct().order_by("given_name", "family_name")
        if see_all
        else [],
        "waiting_total": expenses.filter(status=Expense.Status.WAITING).aggregate(total=Sum("amount_pence"))["total"],
        "query": request.GET.copy(),
    }
    context["query"].pop("page", None)
    return render(request, "expenses/index.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def claim(request):
    if not is_member(request.user):
        raise Http404
    form = ExpenseForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        expense = claims.submit(
            request.user,
            category=data["category"],
            description=data["description"],
            amount_pence=data["amount"],
            spent_on=data["spent_on"],
            receipt=data["receipt"],
        )
        messages.success(request, "Thank you. Your claim has been sent to the trustees.")
        return redirect("expenses:detail", expense.pk)
    return render(request, "expenses/claim.html", {"form": form})


@login_required
def detail(request, expense_id):
    expense = _expense_for(request, expense_id)
    context = {
        "expense": expense,
        "can_decide": claims.can_approve(request.user)
        and expense.status == Expense.Status.WAITING
        and expense.member_id != request.user.pk,
        "own": expense.member_id == request.user.pk,
    }
    return render(request, "expenses/detail.html", context)


CONTENT_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


@login_required
def receipt(request, expense_id):
    expense = _expense_for(request, expense_id)
    if not expense.receipt:
        raise Http404
    suffix = "." + expense.receipt.name.rsplit(".", 1)[-1].lower()
    response = FileResponse(
        expense.receipt.open("rb"),
        content_type=CONTENT_TYPES.get(suffix, "application/octet-stream"),
        filename=f"receipt-{expense.pk}{suffix}",
    )
    response["Cache-Control"] = "private, max-age=600"
    response["Content-Security-Policy"] = "sandbox"  # a PDF can't run anything as this site
    return response


@login_required
@require_POST
def decide(request, expense_id):
    expense = _expense_for(request, expense_id)
    try:
        if request.POST.get("decision") == "approve":
            claims.approve(expense.pk, request.user)
            messages.success(request, f"Approved: £{expense.amount_pence / 100:.2f} added to the member's credit.")
        elif request.POST.get("decision") == "decline":
            claims.decline(expense.pk, request.user, request.POST.get("reason", ""))
            messages.success(request, "Declined. The member has been told why.")
        else:
            raise claims.CantDecide("Please choose approve or decline.")
    except claims.CantDecide as problem:
        messages.error(request, str(problem))
    return redirect("expenses:detail", expense.pk)


@login_required
@require_POST
def withdraw(request, expense_id):
    expense = _expense_for(request, expense_id)
    try:
        claims.withdraw(expense, request.user)
    except claims.CantDecide as problem:
        messages.error(request, str(problem))
        return redirect("expenses:detail", expense.pk)
    messages.success(request, "Your claim has been withdrawn.")
    return redirect("expenses:index")
