from decimal import Decimal

from django import forms
from django.utils import timezone

from apps.payments.forms import PoundsField, to_pence
from apps.site_settings.models import SiteSettings

from . import claims
from .models import Expense


def machines(also=None):
    """Machines to choose from: those in use, and `also` (the one already chosen, even if it's been archived)."""
    from django.db.models import Q

    from apps.equipment.models import Equipment

    return Equipment.objects.filter(Q(archived=False) | Q(pk=also)).order_by("name")


class ExpenseForm(forms.Form):
    category = forms.ChoiceField(choices=[("", "Choose…"), *Expense.Category.choices])
    description = forms.CharField(
        max_length=1000, widget=forms.Textarea(attrs={"rows": 3}), help_text="What you bought, and what it's for."
    )
    amount = PoundsField(label="Amount (£)", max_value=Decimal("10000"))
    spent_on = forms.DateField(
        label="Date of purchase", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
    )
    equipment = forms.ModelChoiceField(
        queryset=None,
        required=False,
        label="Machine",
        empty_label="Not for a particular machine",
        help_text="If it was for one machine (a part, a repair, its consumables), choose it.",
    )
    receipt = forms.FileField(help_text="A photo or scan of the receipt: JPEG, PNG or PDF, up to 10 MB.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["equipment"].queryset = machines()

    def clean_amount(self):
        pence = to_pence(self.cleaned_data["amount"])
        limit = SiteSettings.load().expense_maximum_pence
        if pence > limit:
            raise forms.ValidationError(
                f"You can claim up to £{limit / 100:,.2f}. Please talk to the trustees about bigger spending."
            )
        return pence

    def clean_spent_on(self):
        day = self.cleaned_data["spent_on"]
        if day > timezone.localdate():
            raise forms.ValidationError("The date of purchase can't be in the future.")
        return day

    def clean_receipt(self):
        upload = self.cleaned_data["receipt"]
        try:
            return claims.receipt_file(upload)
        except claims.BadReceipt as problem:
            raise forms.ValidationError(str(problem)) from problem
