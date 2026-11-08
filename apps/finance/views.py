"""
The finance pages (spec 04 §4.6, §4.8): for the `finance` role and admins. Membership statistics are for every
member.
"""

import csv
import re
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from apps.access.models import AccessEvent
from apps.billing.models import Charge
from apps.members.models import Member
from apps.payments import services
from apps.payments.models import Payment
from apps.profiles.views import is_member

from . import figures, money, statements
from .access import can_manage, can_see, require_manage, require_see
from .models import BankLine, BankMatch, BankStatement

PER_PAGE = 50


def find_member(text):
    """The member chosen in a member box: "Name (#12)", "#12", "12" or an email address."""
    text = (text or "").strip()
    if not text:
        return None
    found = re.search(r"#?(\d+)\)?$", text)
    if found:
        return Member.objects.filter(pk=int(found.group(1))).first()
    return Member.objects.filter(email__iexact=text).first()


def member_label(member) -> str:
    return f"{member.full_name} (#{member.pk})" if member else ""


def _members():
    return Member.objects.order_by("given_name", "family_name").only("pk", "given_name", "family_name")


def _month(text):
    try:
        year, month = (int(part) for part in text.split("-"))
        return date(year, month, 1)
    except (ValueError, AttributeError):
        return None


def _next_month(day):
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def _months(since):
    """Month choices from now back to `since`, newest first."""
    month, months = timezone.localdate().replace(day=1), []
    since = (since or month).replace(day=1)
    while month >= since and len(months) < 240:
        months.append(month)
        month = (month - timedelta(days=1)).replace(day=1)
    return months


def _date(text):
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _query(request):
    query = request.GET.copy()
    query.pop("page", None)
    return query


# -- Overview -----------------------------------------------------------------------------------------------------


@login_required
def overview(request):
    require_see(request.user)
    return render(
        request, "finance/overview.html", {"figures": figures.overview(), "can_manage": can_manage(request.user)}
    )


# -- Income, spending and machines (04 §4.9) ----------------------------------------------------------------------


def _period(request, default_month=True):
    """(start, end, label, choices) from ?month=YYYY-MM, ?year=YYYY or ?start=&end=; this month by default."""
    get = request.GET
    today = timezone.localdate()
    choices = {
        "month": get.get("month", ""),
        "year": get.get("year", ""),
        "start": get.get("start", ""),
        "end": get.get("end", ""),
    }
    start, end = _date(choices["start"]), _date(choices["end"])
    if start and end and start <= end:
        return start, end, f"{start:%-d %b %Y} to {end:%-d %b %Y}", choices
    if choices["year"].isdigit() and 2000 <= int(choices["year"]) <= today.year:
        year = int(choices["year"])
        return date(year, 1, 1), date(year, 12, 31), str(year), choices
    month = _month(choices["month"])
    if month is None and not default_month:
        return None, None, "All time", choices
    month = month or today.replace(day=1)
    choices["month"] = f"{month:%Y-%m}"
    return month, _next_month(month) - timedelta(days=1), f"{month:%B %Y}", choices


def _first_payment_day():
    first = Payment.objects.order_by("created_at").values_list("created_at", flat=True).first()
    return timezone.localtime(first).date() if first else None


@login_required
def income(request):
    require_see(request.user)
    start, end, label, choices = _period(request)
    trend = money.months(min(end, timezone.localdate()))
    widest = max([row["income"] for row in trend] + [row["spent"] for row in trend] + [1])
    first = _first_payment_day()
    context = {
        "summary": money.summary(start, end),
        "label": label,
        "choices": choices,
        "months": _months(first),
        "years": range(timezone.localdate().year, (first or timezone.localdate()).year - 1, -1),
        "trend": trend,
        "widest": widest,
        "headings": money.HEADINGS,
    }
    return render(request, "finance/income.html", context)


@login_required
def machines(request):
    require_see(request.user)
    start, end, label, choices = _period(request, default_month=False)
    first = _first_payment_day()
    context = {
        "report": money.machines(start, end),
        "label": label,
        "choices": choices,
        "years": range(timezone.localdate().year, (first or timezone.localdate()).year - 1, -1),
    }
    return render(request, "finance/machines.html", context)


# -- Payments -----------------------------------------------------------------------------------------------------

PAYMENT_SORTS = {
    "newest": ("-created_at", "-pk"),
    "oldest": ("created_at", "pk"),
    "biggest": ("-amount_pence", "-pk"),
    "smallest": ("amount_pence", "pk"),
}


def _filtered_payments(request):
    payments = Payment.objects.select_related("member")
    get = request.GET
    filters = {name: get.get(name, "") for name in ("month", "start", "end", "member", "reason", "source", "status")}
    filters["unmatched"] = get.get("unmatched", "")
    month = _month(filters["month"])
    start, end = _date(filters["start"]), _date(filters["end"])
    if month:
        start, end = month, _next_month(month) - timedelta(days=1)
    if start:
        payments = payments.filter(created_at__date__gte=start)
    if end:
        payments = payments.filter(created_at__date__lte=end)
    if filters["member"]:
        member = find_member(filters["member"])
        payments = payments.filter(member=member) if member else payments.none()
    for name in ("reason", "source", "status"):
        if filters[name]:
            payments = payments.filter(**{name: filters[name]})
    if filters["unmatched"]:
        payments = payments.filter(member__isnull=True)
    sort = get.get("sort", "newest")
    return payments.order_by(*PAYMENT_SORTS.get(sort, PAYMENT_SORTS["newest"])), filters, sort


@login_required
def payments(request):
    require_see(request.user)
    rows, filters, sort = _filtered_payments(request)
    if request.GET.get("format") == "csv":
        return _payments_csv(rows)
    totals = rows.aggregate(amount=Sum("amount_pence"), fees=Sum("fee_pence"), count=Count("pk"))
    first = Payment.objects.order_by("created_at").values_list("created_at", flat=True).first()
    context = {
        "page": Paginator(rows, PER_PAGE).get_page(request.GET.get("page")),
        "filters": filters,
        "sort": sort,
        "sorts": [("newest", "Newest"), ("oldest", "Oldest"), ("biggest", "Biggest"), ("smallest", "Smallest")],
        "months": _months(timezone.localtime(first).date() if first else None),
        "reasons": Payment.Reason.choices,
        "sources": Payment.Source.choices,
        "statuses": Payment.Status.choices,
        "totals": {**totals, "net": (totals["amount"] or 0) - (totals["fees"] or 0)},
        "members": _members(),
        "query": _query(request),
    }
    return render(request, "finance/payments.html", context)


def _payments_csv(rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="payments-{timezone.localdate():%Y-%m-%d}.csv"'
    writer = csv.writer(response)
    writer.writerow(["Date", "Member", "Member number", "For", "Paid by", "Status", "Amount", "Fee", "Reference"])
    for payment in rows.iterator():
        member = payment.member
        writer.writerow(
            [
                timezone.localtime(payment.created_at).strftime("%Y-%m-%d %H:%M"),
                _safe(member.full_name) if member else "",
                member.pk if member else "",
                payment.get_reason_display(),
                payment.get_source_display(),
                payment.get_status_display(),
                f"{payment.amount_pence / 100:.2f}",
                f"{payment.fee_pence / 100:.2f}",
                _safe(payment.reference),
            ]
        )
    return response


def _safe(text) -> str:
    """Stop a spreadsheet treating a cell as a formula."""
    text = str(text)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@login_required
def payment(request, payment_id):
    require_see(request.user)
    item = get_object_or_404(
        Payment.objects.select_related("member", "charge", "usage_session__equipment", "corrects", "created_by"),
        pk=payment_id,
    )
    manage = can_manage(request.user)
    context = {
        "payment": item,
        "corrections": item.corrections.select_related("created_by"),
        "bank_line": BankLine.objects.filter(payment=item).first(),
        "can_assign": manage and services.is_unmatched(item),
        "can_refund": manage and services.can_refund_to_credit(item) and not item.corrections.exists(),
        "can_cancel": manage and services.can_cancel(item) and not item.corrections.exists(),
        "members": _members() if manage and services.is_unmatched(item) else [],
    }
    return render(request, "finance/payment.html", context)


@login_required
@require_POST
def put_right(request, payment_id):
    require_manage(request.user)
    item = get_object_or_404(Payment, pk=payment_id)
    change, note = request.POST.get("change"), request.POST.get("note", "").strip()[:500]
    try:
        if change == "assign":
            member = find_member(request.POST.get("member"))
            if member is None:
                raise services.CantCorrect("Please choose the member this money is from.")
            services.assign_unmatched(item, member, request.user)
            messages.success(request, f"Added to {member.full_name}'s credit.")
        elif change == "refund":
            services.refund_to_credit(item, request.user, note)
            messages.success(request, "Refunded to their credit.")
        elif change == "cancel":
            services.cancel_payment(item, request.user, note)
            messages.success(request, "Cancelled. A correcting payment has been added.")
        else:
            raise Http404
    except services.CantCorrect as error:
        messages.error(request, str(error))
    return redirect("finance:payment", item.pk)


# -- Charges ------------------------------------------------------------------------------------------------------


@login_required
def charges(request):
    require_see(request.user)
    rows = Charge.objects.select_related("member", "rental__locker")
    filters = {name: request.GET.get(name, "") for name in ("status", "kind", "month", "member")}
    if filters["status"] in Charge.Status.values:
        rows = rows.filter(status=filters["status"])
    if filters["kind"] in Charge.Kind.values:
        rows = rows.filter(kind=filters["kind"])
    month = _month(filters["month"])
    if month:
        rows = rows.filter(charge_date__gte=month, charge_date__lt=_next_month(month))
    if filters["member"]:
        member = find_member(filters["member"])
        rows = rows.filter(member=member) if member else rows.none()
    first = Charge.objects.order_by("charge_date").values_list("charge_date", flat=True).first()
    context = {
        "page": Paginator(rows.order_by("-charge_date", "-pk"), PER_PAGE).get_page(request.GET.get("page")),
        "filters": filters,
        "statuses": Charge.Status.choices,
        "kinds": Charge.Kind.choices,
        "months": _months(first),
        "members": _members(),
        "query": _query(request),
    }
    return render(request, "finance/charges.html", context)


# -- Bank statements ----------------------------------------------------------------------------------------------


@login_required
@require_http_methods(["GET", "POST"])
def statements_page(request):
    require_see(request.user)
    if request.method == "POST":
        require_manage(request.user)
        upload = request.FILES.get("statement")
        if upload is None:
            messages.error(request, "Please choose the statement file.")
        else:
            try:
                statement = statements.read_statement(upload, request.user)
            except statements.BadStatement as error:
                messages.error(request, str(error))
            else:
                return redirect("finance:statement", statement.pk)
    history = (
        BankStatement.objects.select_related("uploaded_by", "recorded_by")
        .annotate(
            credits=Count("lines"),
            total=Sum("lines__amount_pence"),
            unmatched=Count("lines", filter=Q(lines__outcome=BankLine.Outcome.UNMATCHED)),
        )
        .order_by("-uploaded_at", "-pk")
    )
    context = {"page": Paginator(history, 25).get_page(request.GET.get("page")), "can_manage": can_manage(request.user)}
    return render(request, "finance/statements.html", context)


def _line_rows(statement, data=None):
    rows = []
    for line in statement.lines.select_related("suggested_member", "member", "payment"):
        action = (data or {}).get(f"line-{line.pk}-action") or statements.default_action(line)
        member_text = (data or {}).get(f"line-{line.pk}-member")
        if member_text is None:
            member_text = member_label(line.suggested_member)
        rows.append(
            {
                "line": line,
                "action": action,
                "member_text": member_text,
                "actions": statements.actions_for(line),
                "duplicate": line.outcome == BankLine.Outcome.DUPLICATE,
            }
        )
    return rows


@login_required
@require_http_methods(["GET", "POST"])
def statement(request, statement_id):
    require_see(request.user)
    item = get_object_or_404(BankStatement, pk=statement_id)
    data = None
    if request.method == "POST":
        require_manage(request.user)
        if request.POST.get("do") == "discard":
            if item.is_draft:
                item.delete()
                messages.info(request, "The statement was thrown away. Nothing was recorded.")
            return redirect("finance:statements")
        data = request.POST
        decisions, problems = {}, []
        for line in item.lines.exclude(outcome=BankLine.Outcome.DUPLICATE):
            action = data.get(f"line-{line.pk}-action", "")
            text = data.get(f"line-{line.pk}-member", "")
            member = find_member(text)
            if action in (statements.SUBSCRIPTION, statements.BALANCE) and member is None:
                problems.append(f"Row {line.row}: " + ("we don't know that member." if text.strip() else "who paid?"))
            decisions[line.pk] = (action, member)
        if not problems:
            try:
                counts = statements.record(item, decisions, request.user)
            except statements.CantRecord as error:
                problems.append(str(error))
            else:
                recorded = counts.get(BankLine.Outcome.RECORDED, 0)
                messages.success(request, f"Recorded {recorded} payment{'s' if recorded != 1 else ''}.")
                return redirect("finance:statement", item.pk)
        for problem in problems:
            messages.error(request, problem)
    rows = _line_rows(item, data)
    context = {
        "statement": item,
        "rows": rows,
        "total": sum(r["line"].amount_pence for r in rows),
        "can_manage": can_manage(request.user),
        "members": _members() if item.is_draft else [],
    }
    return render(request, "finance/statement.html", context)


@login_required
def bank_lines(request):
    require_see(request.user)
    rows = BankLine.objects.filter(kept=True).select_related("member", "payment", "statement")
    filters = {name: request.GET.get(name, "") for name in ("outcome", "kind", "month")}
    if filters["outcome"] in BankLine.Outcome.values and filters["outcome"]:
        rows = rows.filter(outcome=filters["outcome"])
    if filters["kind"] in BankLine.Kind.values:
        rows = rows.filter(kind=filters["kind"])
    month = _month(filters["month"])
    if month:
        rows = rows.filter(date__gte=month, date__lt=_next_month(month))
    first = BankLine.objects.filter(kept=True).order_by("date").values_list("date", flat=True).first()
    context = {
        "page": Paginator(rows.order_by("-date", "-row"), PER_PAGE).get_page(request.GET.get("page")),
        "filters": filters,
        "outcomes": [c for c in BankLine.Outcome.choices if c[0] not in ("", BankLine.Outcome.DUPLICATE)],
        "kinds": BankLine.Kind.choices,
        "months": _months(first),
        "total": rows.aggregate(total=Sum("amount_pence"))["total"] or 0,
        "query": _query(request),
    }
    return render(request, "finance/bank_lines.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def bank_line(request, line_id):
    require_see(request.user)
    line = get_object_or_404(BankLine.objects.select_related("statement", "member", "payment"), pk=line_id, kept=True)
    open_to_match = line.outcome in (BankLine.Outcome.UNMATCHED, BankLine.Outcome.NOT_MEMBER)
    member_text = member_label(line.suggested_member)
    if request.method == "POST":
        require_manage(request.user)
        member_text = request.POST.get("member", "")
        member = find_member(member_text)
        action = request.POST.get("action", "")
        try:
            if action in (statements.SUBSCRIPTION, statements.BALANCE) and member is None:
                raise statements.CantRecord("Please choose the member who paid.")
            statements.match_later(line, action, member, request.user)
        except statements.CantRecord as error:
            messages.error(request, str(error))
        else:
            messages.success(request, "Done.")
            return redirect("finance:bank-line", line.pk)
    context = {
        "line": line,
        "can_match": open_to_match and can_manage(request.user),
        "member_text": member_text,
        "actions": [a for a in statements.actions_for(line) if a[0] != statements.UNMATCHED],
        "members": _members() if open_to_match else [],
    }
    return render(request, "finance/bank_line.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def matches(request):
    require_see(request.user)
    if request.method == "POST":
        require_manage(request.user)
        if request.POST.get("forget"):
            BankMatch.objects.filter(pk=request.POST["forget"]).delete()
            messages.info(request, "Forgotten.")
        else:
            text, member = statements.tidy(request.POST.get("text", "")), find_member(request.POST.get("member"))
            if len(text) < 3 or member is None:
                messages.error(request, "Please give at least three letters of text, and the member.")
            else:
                BankMatch.objects.update_or_create(
                    text=text, defaults={"member": member, "whole": False, "created_by": request.user}
                )
                messages.success(request, f"Lines containing “{text}” will be matched to {member.full_name}.")
        return redirect("finance:matches")
    context = {
        "matches": BankMatch.objects.select_related("member").order_by("whole", "text"),
        "can_manage": can_manage(request.user),
        "members": _members(),
    }
    return render(request, "finance/matches.html", context)


# -- Statistics (any member) --------------------------------------------------------------------------------------

COUNTED = (Member.Status.ACTIVE, Member.Status.LEAVING)


@login_required
def stats(request):
    if not is_member(request.user):
        raise Http404
    members = Member.objects.filter(status__in=COUNTED)
    methods = members.values("payment_method").annotate(count=Count("pk")).order_by("-count")
    labels = dict(Member.PaymentMethod.choices)
    by_method = [{"label": labels.get(row["payment_method"]) or "None", "count": row["count"]} for row in methods]
    amounts = [m for m in members.values_list("monthly_subscription_pence", flat=True) if m]
    buckets = {}
    for pence in amounts:
        pounds = max(5, int(5 * round(pence / 500)))
        buckets[pounds] = buckets.get(pounds, 0) + 1
    spread = [{"label": f"£{p}", "count": buckets[p]} for p in sorted(buckets)]
    now = timezone.now()

    def came_in(days):
        return (
            AccessEvent.objects.filter(
                occurred_at__gte=now - timedelta(days=days),
                kind__in=(AccessEvent.Kind.ENTRY, AccessEvent.Kind.CHECK_IN),
                member__isnull=False,
            )
            .values("member")
            .distinct()
            .count()
        )

    widest = max([row["count"] for row in by_method + spread] or [1])
    context = {
        "members": members.count(),
        "honorary": Member.objects.filter(status=Member.Status.HONORARY).count(),
        "by_method": by_method,
        "spread": spread,
        "widest": widest,
        "average": round(sum(amounts) / len(amounts)) if amounts else None,
        "came_in_month": came_in(30),
        "came_in_quarter": came_in(91),
        "finance_link": reverse("finance:overview") if can_see(request.user) else "",
    }
    return render(request, "finance/stats.html", context)
