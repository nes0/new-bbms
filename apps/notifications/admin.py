from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """Read-only: what members have been told. Members mark their own as read on their page."""

    list_display = ("created_at", "member", "kind", "short_message", "unread")
    list_filter = ("unread", "kind")
    search_fields = ("member__email", "member__given_name", "member__family_name", "message", "key")
    date_hierarchy = "created_at"
    autocomplete_fields = ("member",)

    @admin.display(description="Message")
    def short_message(self, notification):
        return notification.message[:80]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
