"""
Group email in the admin (spec 06 §6.6): the rules for who may email which group, and a read-only record of what was
sent. Emails are written and sent on the group email page (/group-email/).
"""

from django import forms
from django.contrib import admin

from . import audiences
from .models import GroupEmail, SenderRule


class SenderRuleForm(forms.ModelForm):
    category = forms.ChoiceField(
        label="Machine", required=False, help_text=SenderRule._meta.get_field("category").help_text
    )

    class Meta:
        model = SenderRule
        fields = ["sender", "sender_role", "audience", "audience_role", "category"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "—"), *sorted(audiences.machine_names().items(), key=lambda item: item[1].lower())]
        current = self.instance.category
        if current and current not in dict(choices):
            choices.append((current, f"{current} (no machine needs training for it now)"))
        self.fields["category"].choices = choices


@admin.register(SenderRule)
class SenderRuleAdmin(admin.ModelAdmin):
    form = SenderRuleForm
    list_display = ["__str__", "sender", "audience"]
    list_filter = ["sender", "audience"]
    fieldsets = [
        (
            None,
            {
                "description": "Admins can email every group. Anyone else needs a rule here, and must be a current "
                "member. Members send from <a href='/group-email/'>Group email</a>.",
                "fields": ["sender", "sender_role", "audience", "audience_role", "category"],
            },
        )
    ]


@admin.register(GroupEmail)
class GroupEmailAdmin(admin.ModelAdmin):
    list_display = ["sent_at", "subject", "sent_by", "audience_label", "recipients", "failed"]
    list_filter = ["sent_at"]
    search_fields = ["subject", "body", "sent_by__given_name", "sent_by__family_name", "sent_by__email"]
    date_hierarchy = "sent_at"
    list_select_related = ["sent_by"]
    fields = readonly_fields = ["sent_at", "sent_by", "audience_label", "recipients", "failed", "subject", "body"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
