"""
Fobs, readers and the door log in the admin (spec 05 §5.1-5.4, 06 §6.10).

Fobs are issued through "Issue a fob" or "Tap new fob on a reader" (both go through apps/access/fobs.py, which holds
the rules), never by editing a fob's number. Who may do this is a permission: "access | key fob | Can add key fob"
(give it to a role's group, e.g. comms). Marking lost needs "Can change key fob".
"""

from django.contrib import admin, messages
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from apps.members.admin import MemberAdmin

from . import fobs, tags
from .forms import IssueFobForm, OpenEnrolmentForm
from .models import AccessEvent, Device, FobEnrolment, KeyFob


def _card_label(fob):
    return tags.format_card_number(fob.card_number) if fob.card_number is not None else "—"


class KeyFobInline(admin.TabularInline):
    """A member's fobs, read-only, with buttons to issue another."""

    model = KeyFob
    fk_name = "member"
    extra = 0
    fields = ("tag_id", "card_number_display", "active", "issued_at", "issued_by", "lost_at")
    readonly_fields = fields
    show_change_link = True
    can_delete = False
    template = "admin/access/keyfob/inline.html"

    @admin.display(description="Card number")
    def card_number_display(self, fob):
        return _card_label(fob)

    def has_add_permission(self, request, obj=None):
        return False  # the inline's buttons go to "Issue a fob" and "Tap new fob on a reader" instead


# Show a member's fobs on their admin page.
MemberAdmin.inlines = [*MemberAdmin.inlines, KeyFobInline]


@admin.register(KeyFob)
class KeyFobAdmin(admin.ModelAdmin):
    list_display = ("__str__", "card_number_display", "member", "active", "issued_at", "issued_by")
    list_filter = ("active",)
    search_fields = ("tag_id", "member__email", "member__given_name", "member__family_name")
    list_select_related = ("member", "issued_by")
    readonly_fields = [f.name for f in KeyFob._meta.fields]
    actions = ["mark_lost", "reactivate"]

    @admin.display(description="Card number")
    def card_number_display(self, fob):
        return _card_label(fob)

    def get_search_results(self, request, queryset, search_term):
        queryset, may_have_duplicates = super().get_search_results(request, queryset, search_term)
        digits = search_term.strip()
        if digits.isdigit() and len(digits) <= 10:  # the number printed on the fob
            queryset |= self.model.objects.filter(card_number=int(digits))
        return queryset, may_have_duplicates

    @admin.action(description="Mark selected fobs as lost", permissions=["change"])
    def mark_lost(self, request, queryset):
        count = 0
        for fob in queryset.filter(active=True):
            fob.mark_lost()
            count += 1
        self.message_user(
            request, f"Marked {count} fob{'s' if count != 1 else ''} as lost. Readers drop them at their next check-in."
        )

    @admin.action(description="Found again: reactivate selected fobs", permissions=["change"])
    def reactivate(self, request, queryset):
        for fob in queryset.filter(active=False).select_related("member"):
            try:
                fobs.reactivate_fob(fob)
                self.message_user(request, f"Reactivated {fob} for {fob.member}.")
            except fobs.FobError as error:
                self.message_user(request, f"{fob}: {error}", messages.ERROR)

    # --- issuing ---

    def get_urls(self):
        view = self.admin_site.admin_view
        return [
            path("issue/", view(self.issue_view), name="access_keyfob_issue"),
            path("enrol/", view(self.enrol_view), name="access_keyfob_enrol"),
            path("enrol/<int:pk>/", view(self.enrolment_view), name="access_keyfob_enrolment"),
            *super().get_urls(),
        ]

    def add_view(self, request, form_url="", extra_context=None):
        return redirect(reverse("admin:access_keyfob_issue") + (f"?{request.GET.urlencode()}" if request.GET else ""))

    def _context(self, request, title, **extra):
        return {**self.admin_site.each_context(request), "title": title, "opts": self.model._meta, **extra}

    def _initial_member(self, request):
        return {"member": request.GET["member"]} if request.GET.get("member", "").isdigit() else {}

    def _back_to_member(self, member):
        return redirect("admin:members_member_change", member.pk)

    def issue_view(self, request):
        if not self.has_add_permission(request):
            return redirect("admin:index")
        form = IssueFobForm(request.POST or None, initial=self._initial_member(request), admin_site=self.admin_site)
        if request.method == "POST" and form.is_valid():
            data = form.cleaned_data
            try:
                fob = fobs.issue_fob(
                    data["member"], tag_id=data["tag_id"], card_number=data["card_number"], issued_by=request.user
                )
            except fobs.FobError as error:  # someone else got there first
                form.add_error(None, str(error))
            else:
                self.log_addition(request, fob, [{"added": {}}])
                self.message_user(request, f"Issued fob {_card_label(fob)} to {fob.member}.", messages.SUCCESS)
                return self._back_to_member(fob.member)
        return TemplateResponse(
            request, "admin/access/keyfob/issue.html", self._context(request, "Issue a fob", form=form)
        )

    def enrol_view(self, request):
        if not self.has_add_permission(request):
            return redirect("admin:index")
        form = OpenEnrolmentForm(
            request.POST or None, initial=self._initial_member(request), admin_site=self.admin_site
        )
        if request.method == "POST" and form.is_valid():
            try:
                enrolment = fobs.open_enrolment(form.cleaned_data["member"], form.cleaned_data["device"], request.user)
            except fobs.FobError as error:
                form.add_error(None, str(error))
            else:
                return redirect("admin:access_keyfob_enrolment", enrolment.pk)
        context = self._context(request, "Tap a new fob on a reader", form=form)
        return TemplateResponse(request, "admin/access/keyfob/enrol.html", context)

    def enrolment_view(self, request, pk):
        """The waiting page. HTMX asks for it again every 2 seconds until something has been tapped."""
        if not self.has_add_permission(request):
            return redirect("admin:index")
        enrolment = get_object_or_404(FobEnrolment.objects.select_related("member", "device", "opened_by"), pk=pk)
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "confirm":
                try:
                    fob = fobs.confirm_enrolment(enrolment, request.user)
                except fobs.FobError as error:
                    self.message_user(request, str(error), messages.ERROR)
                    return redirect("admin:access_keyfob_enrolment", pk)
                self.log_addition(request, fob, [{"added": {}}])
                self.message_user(request, f"Issued fob {_card_label(fob)} to {fob.member}.", messages.SUCCESS)
                return self._back_to_member(fob.member)
            if action == "cancel":
                fobs.cancel_enrolment(enrolment)
                self.message_user(request, "Cancelled: nothing was issued.")
                return self._back_to_member(enrolment.member)
        template = (
            "admin/access/keyfob/enrolment_status.html"
            if request.headers.get("HX-Request")
            else "admin/access/keyfob/enrolment.html"
        )
        context = self._context(request, f"New fob for {enrolment.member}", enrolment=enrolment)
        return TemplateResponse(request, template, context)


@admin.register(FobEnrolment)
class FobEnrolmentAdmin(admin.ModelAdmin):
    """The audit trail of tap-to-enrol windows (05 §5.1)."""

    list_display = ("opened_at", "member", "device", "opened_by", "state_display", "captured_label", "confirmed_by")
    list_filter = ("device",)
    search_fields = ("member__email", "member__given_name", "member__family_name")
    list_select_related = ("member", "device", "opened_by", "confirmed_by")
    date_hierarchy = "opened_at"

    @admin.display(description="State")
    def state_display(self, enrolment):
        return enrolment.get_state_display()

    @admin.display(description="Captured card number")
    def captured_label(self, enrolment):
        return enrolment.captured_label or "—"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("name", "device_id", "role", "enabled", "firmware_version", "last_heartbeat", "cache_status")
    list_filter = ("role", "monitor_heartbeat", "enabled")
    readonly_fields = (
        "last_boot",
        "last_heartbeat",
        "firmware_version",
        "reported_cache_version",
        "cache_status",
        "pending_commands",
        "legacy_id",
    )
    actions = ["new_api_key", "queue_refresh_cache", "queue_purge_cache", "queue_reboot", "disable", "enable"]

    @admin.display(description="Cache")
    def cache_status(self, device):
        from .api_v2 import ApiError, allowed_fob_list, cache_version

        try:
            current = cache_version(allowed_fob_list(device))
        except ApiError:  # a machine reader with no machine has no list
            return "—"
        if not device.reported_cache_version:
            return "not reported"
        return "up to date" if device.reported_cache_version == current else f"out of date ({current} now)"

    def _queue(self, request, queryset, command):
        for device in queryset:
            device.queue_command(command)
        self.message_user(request, f'Queued "{command}"; it is sent at each device\'s next check-in.')

    @admin.action(description='Queue "refresh cache"')
    def queue_refresh_cache(self, request, queryset):
        self._queue(request, queryset, "refresh_cache")

    @admin.action(description='Queue "purge cache"')
    def queue_purge_cache(self, request, queryset):
        self._queue(request, queryset, "purge_cache")

    @admin.action(description='Queue "reboot"')
    def queue_reboot(self, request, queryset):
        self._queue(request, queryset, "reboot")

    @admin.action(description="Disable (v2 readers refuse everyone)")
    def disable(self, request, queryset):
        queryset.update(enabled=False)

    @admin.action(description="Enable")
    def enable(self, request, queryset):
        queryset.update(enabled=True)

    @admin.action(description="Generate a new API key (the old one stops working)")
    def new_api_key(self, request, queryset):
        for device in queryset:
            key = device.generate_api_key()
            device.save(update_fields=["api_key_hash"])
            # Shown once only: BBMS keeps just a hash of the key.
            self.message_user(request, f"New API key for {device.device_id}: {key}", messages.WARNING)

    def save_model(self, request, obj, form, change):
        if not change:
            key = obj.generate_api_key()
            message = f"API key for {obj.device_id} (shown once, copy it now): {key}"
            self.message_user(request, message, messages.WARNING)
        super().save_model(request, obj, form, change)


@admin.register(AccessEvent)
class AccessEventAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "kind", "member", "device", "reason", "delayed")
    list_filter = ("kind", "device", "delayed")
    search_fields = ("member__email", "member__given_name", "member__family_name")
    date_hierarchy = "occurred_at"

    # The log is a record of what happened: read-only.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
