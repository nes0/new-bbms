"""
Writing proposals (trustees, spec 06 §6.3). A proposal can be changed or deleted only until voting starts. The
admin shows who has voted, never how: the ballot is secret.
"""

from datetime import timedelta

from django import forms
from django.contrib import admin
from django.db.models import Count
from django.utils import timezone
from django.utils.html import format_html_join

from .models import Proposal

DEFAULT_DAYS = 3  # legacy's default voting window: today until three days from now


class ProposalForm(forms.ModelForm):
    class Meta:
        model = Proposal
        fields = ["title", "description", "start_date", "end_date"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 18, "cols": 90}),
            "start_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "end_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        today = timezone.localdate()
        if start and start < today:
            self.add_error("start_date", "Voting can't start in the past.")
        if start and end and end < start:
            self.add_error("end_date", "Voting must end on or after the day it starts.")
        return cleaned


@admin.register(Proposal)
class ProposalAdmin(admin.ModelAdmin):
    form = ProposalForm
    list_display = ["title", "start_date", "end_date", "state", "voted"]
    search_fields = ["title"]
    date_hierarchy = "end_date"
    RESULT_FIELDS = [
        "voted",
        "voters",
        "eligible_voters",
        "quorum_percent",
        "votes_for",
        "votes_against",
        "votes_neutral",
        "abstentions",
        "quorum_met",
        "passed",
        "opened_at",
        "counted_at",
        "created_by",
    ]

    def get_changeform_initial_data(self, request):
        today = timezone.localdate()
        return {"start_date": today, "end_date": today + timedelta(days=DEFAULT_DAYS)}

    def get_fields(self, request, obj=None):
        fields = ["title", "description", "start_date", "end_date"]
        return fields if obj is None else fields + self.RESULT_FIELDS

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return []
        if obj.has_started():
            return ["title", "description", "start_date", "end_date", *self.RESULT_FIELDS]
        return self.RESULT_FIELDS

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.has_started():
            return False  # members may have voted: it's part of the record
        return super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(vote_count=Count("votes"))

    @admin.display(description="state")
    def state(self, obj):
        status = obj.status()
        if status == "closed":
            return obj.outcome() or "closed, not counted yet"
        return status

    @admin.display(description="members who voted", ordering="vote_count")
    def voted(self, obj):
        return obj.vote_count

    @admin.display(description="who voted (not how)")
    def voters(self, obj):
        names = sorted(obj.votes.select_related("member").values_list("member__given_name", "member__family_name"))
        return format_html_join(", ", "{} {}", names) or "Nobody yet"
