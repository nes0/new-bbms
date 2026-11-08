from django.contrib import admin

from .models import MoodleAccount


@admin.register(MoodleAccount)
class MoodleAccountAdmin(admin.ModelAdmin):
    """Which Moodle user each member is, as BBMS found them. Read-only: BBMS looks them up itself."""

    list_display = ("member", "moodle_user_id", "found_at")
    search_fields = ("member__email", "member__given_name", "member__family_name")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
