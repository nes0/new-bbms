from django.contrib import admin

from .models import Feedback


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    """Read-only: it's what members sent."""

    list_display = ("sent_at", "member", "short")
    search_fields = ("comments", "member__given_name", "member__family_name")
    readonly_fields = ("member", "comments", "sent_at")

    @admin.display(description="Comments")
    def short(self, feedback):
        return feedback.comments[:100]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
