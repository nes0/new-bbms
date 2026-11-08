import re
from decimal import Decimal

from django import forms

from apps.equipment.models import Equipment
from apps.members.models import Member

from . import prices, services
from .credit import credit_pence
from .models import Payment, WithdrawalRequest


def to_pence(pounds: Decimal) -> int:
    return int((pounds * 100).quantize(Decimal("1")))


class PoundsField(forms.DecimalField):
    def __init__(self, **kwargs):
        kwargs.setdefault("min_value", Decimal("0.01"))
        kwargs.setdefault("max_value", Decimal("10000"))
        super().__init__(max_digits=8, decimal_places=2, **kwargs)


def what_choices():
    """What an admin can record. Built each time, so the labels show the current prices from Settings."""
    return [
        ("subscription", "Monthly subscription (pays what's owed, or a month from today)"),
        ("topup", "Credit top-up (cash received)"),
        ("withdrawal", "Cash paid out of the member's credit"),
        ("induction", "Induction fee"),
        ("key-deposit", f"Key deposit (£{prices.key_deposit_pence() / 100:.2f})"),
        ("storage-box", f"Box fee (£{prices.box_fee_pence() / 100:.2f})"),
    ]


class RecordPaymentForm(forms.Form):
    """Cash and manual payments recorded by an admin (04 §4.5).

    For fixed-price things (induction fees, key deposit, box fee) the amount comes from the equipment or the price
    settings, never from this form.
    """

    PAID_HOW = [
        (Payment.Source.CASH, "Cash"),
        (Payment.Source.MANUAL, "Paid outside BBMS (e.g. bank transfer)"),
        (Payment.Source.BALANCE, "From the member's credit"),
    ]

    member = forms.ModelChoiceField(queryset=Member.objects.order_by("given_name", "family_name"))
    what = forms.ChoiceField(choices=what_choices)
    paid_how = forms.ChoiceField(
        choices=PAID_HOW,
        required=False,
        help_text="For subscriptions, induction fees, key deposits and box fees.",
    )
    equipment = forms.ModelChoiceField(
        queryset=Equipment.objects.filter(archived=False, induction_fee_pence__gt=0),
        required=False,
        help_text="For induction fees: the machine (its fee is used).",
    )
    amount = PoundsField(required=False, label="Amount (£)", help_text="For top-ups and withdrawals only.")
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def clean(self):
        cleaned = super().clean()
        what = cleaned.get("what")
        if what in ("topup", "withdrawal"):
            if not cleaned.get("amount"):
                self.add_error("amount", "Enter the amount.")
        else:
            if cleaned.get("amount"):
                self.add_error("amount", "Leave this empty: the price is set by BBMS.")
            if not cleaned.get("paid_how"):
                self.add_error("paid_how", "Say how it was paid.")
        if what == "induction" and not cleaned.get("equipment"):
            self.add_error("equipment", "Choose the machine.")
        member = cleaned.get("member")
        if what == "subscription" and member is not None and not member.monthly_subscription_pence:
            self.add_error("member", "This member has no monthly subscription amount: set one on their page first.")
        return cleaned

    def save(self, created_by) -> Payment:
        """Record it. Raises services.InsufficientCredit if it's paid from credit the member doesn't have."""
        data = self.cleaned_data
        member, what, how = data["member"], data["what"], data.get("paid_how")
        extra = {"created_by": created_by, "note": data.get("note", "")}
        if what == "topup":
            return services.record_payment(
                member, Payment.Reason.BALANCE, Payment.Source.CASH, to_pence(data["amount"]), **extra
            )
        if what == "withdrawal":
            return services.pay_from_credit(member, Payment.Reason.WITHDRAWAL, to_pence(data["amount"]), **extra)
        if what == "subscription":
            from django.utils import timezone

            from apps.billing.billing import pay_subscription_manually

            return pay_subscription_manually(member, how, timezone.localdate(), **extra)
        if what == "induction":
            return services.pay_induction(member, data["equipment"], how, **extra)
        if what == "key-deposit":
            return services.pay_key_deposit(member, how, **extra)
        return services.pay_box_fee(member, how, **extra)


class WithdrawalRequestForm(forms.ModelForm):
    """A member asks for some of their credit to be paid to their bank account (04 §4.7)."""

    amount = PoundsField(label="Amount (£)")

    class Meta:
        model = WithdrawalRequest
        fields = ["amount", "account_name", "sort_code", "account_number"]
        labels = {"account_name": "Name on the account"}

    def __init__(self, *args, member, **kwargs):
        super().__init__(*args, **kwargs)
        self.member = member
        for name in ("account_name", "sort_code", "account_number"):
            self.fields[name].required = True
        self.fields["sort_code"].widget.attrs.update(placeholder="12-34-56", inputmode="numeric")
        self.fields["account_number"].widget.attrs.update(placeholder="12345678", inputmode="numeric")

    def clean_sort_code(self):
        digits = re.sub(r"[\s-]", "", self.cleaned_data["sort_code"])
        if not re.fullmatch(r"\d{6}", digits):
            raise forms.ValidationError("A sort code is 6 digits, like 12-34-56.")
        return f"{digits[:2]}-{digits[2:4]}-{digits[4:]}"

    def clean_account_number(self):
        digits = re.sub(r"\s", "", self.cleaned_data["account_number"])
        if not re.fullmatch(r"\d{8}", digits):
            raise forms.ValidationError("An account number is 8 digits.")
        return digits

    def clean(self):
        cleaned = super().clean()
        if WithdrawalRequest.objects.filter(member=self.member, status=WithdrawalRequest.Status.REQUESTED).exists():
            raise forms.ValidationError("You already have a withdrawal waiting to be paid.")
        amount = cleaned.get("amount")
        if amount and to_pence(amount) > credit_pence(self.member):
            self.add_error("amount", "That's more than your credit.")
        return cleaned

    def save(self, commit=True):
        request = super().save(commit=False)
        request.member = self.member
        request.amount_pence = to_pence(self.cleaned_data["amount"])
        if commit:
            request.save()
        return request
