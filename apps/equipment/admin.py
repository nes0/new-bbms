from django import forms
from django.contrib import admin
from django.utils import timezone

from . import rules
from .models import Equipment, Induction, MachineStatusChange, ServiceRecord, TrainingCompletion, UsageSession


class ServiceRecordInline(admin.TabularInline):
    model = ServiceRecord
    extra = 0
    fields = ("serviced_at", "note", "recorded_by")
    autocomplete_fields = ("recorded_by",)


class MachineStatusChangeInline(admin.TabularInline):
    model = MachineStatusChange
    extra = 0
    can_delete = False
    fields = ("created_at", "status", "note", "source", "reported_by")
    readonly_fields = fields
    max_num = 0
    verbose_name_plural = "Status history"

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Equipment)
class EquipmentAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "status", "requires_induction", "usage_cost_pence_per_hour", "team", "archived")
    list_filter = ("status", "requires_induction", "archived", "team")
    search_fields = ("name", "slug", "asset_tag_id")
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ("permaloan_member",)
    inlines = [ServiceRecordInline, MachineStatusChangeInline]
    fieldsets = (
        (None, {"fields": ("name", "slug", "team", "room", "description", "help_text", "ppe")}),
        ("Status", {"fields": ("status", "status_note", "status_since")}),
        (
            "Training and charges",
            {
                "fields": (
                    "requires_induction",
                    "induction_category",
                    "induction_fee_pence",
                    "usage_cost_pence_per_hour",
                    "moodle_course_id",
                    "moodle_course_since",
                    "course_required_for_existing",
                )
            },
        ),
        (
            "Fob reader and hour meter",
            {
                "fields": (
                    "meter_mode",
                    "current_threshold_amps",
                    "idle_auto_end_minutes",
                    "service_interval_hours",
                    "total_hours",
                    "hours_since_service_display",
                    "service_due_display",
                    "config_version",
                )
            },
        ),
        (
            "Asset",
            {
                "classes": ("collapse",),
                "fields": (
                    "manufacturer",
                    "model_number",
                    "serial_number",
                    "asset_tag_id",
                    "permaloan_member",
                    "obtained_at",
                    "removed_at",
                    "archived",
                    "legacy_id",
                ),
            },
        ),
    )
    readonly_fields = (
        "legacy_id",
        "total_hours",
        "hours_since_service_display",
        "service_due_display",
        "config_version",
    )

    @admin.display(description="Running hours (total)")
    def total_hours(self, equipment):
        return f"{rules.running_seconds(equipment) / 3600:.1f}" if equipment.pk else "—"

    @admin.display(description="Hours since service")
    def hours_since_service_display(self, equipment):
        return f"{rules.hours_since_service(equipment):.1f}" if equipment.pk else "—"

    @admin.display(description="Service due", boolean=True)
    def service_due_display(self, equipment):
        return rules.service_due(equipment) if equipment.pk else False

    def save_model(self, request, obj, form, change):
        if change and "status" in form.changed_data:
            obj.status_since = timezone.now()
        super().save_model(request, obj, form, change)
        if change and "status" in form.changed_data:
            MachineStatusChange.objects.create(
                equipment=obj,
                status=obj.status,
                note=obj.status_note,
                reported_by=request.user,
                source=MachineStatusChange.Source.ADMIN,
            )


@admin.register(Induction)
class InductionAdmin(admin.ModelAdmin):
    list_display = ("member", "category", "paid", "trained_at", "trainer", "is_trainer")
    list_filter = ("category", "paid", "is_trainer")
    search_fields = ("member__email", "member__given_name", "member__family_name", "category")
    autocomplete_fields = ("member", "trainer")


@admin.register(TrainingCompletion)
class TrainingCompletionAdmin(admin.ModelAdmin):
    list_display = ("member", "moodle_course_id", "completed_at", "last_checked_at")
    search_fields = ("member__email", "member__given_name", "member__family_name")
    autocomplete_fields = ("member",)


class UsageSessionForm(forms.ModelForm):
    class Meta:
        model = UsageSession
        fields = "__all__"  # noqa: DJ007 - the admin's fieldsets choose what is shown

    def clean(self):
        cleaned = super().clean()
        if not self.instance.pk:
            return cleaned
        started, ended = cleaned.get("started_at"), cleaned.get("ended_at")
        times_changed = started != self.instance.started_at or ended != self.instance.ended_at
        if not times_changed:
            return cleaned
        # Adjusting a session's times (05 §5.6).
        if not cleaned.get("adjusted_reason"):
            self.add_error("adjusted_reason", "Give a reason for changing the times.")
        if ended and started and ended <= started:
            self.add_error("ended_at", "The end must be after the start.")
        if ended and ended > timezone.now():
            self.add_error("ended_at", "The end can't be in the future.")
        if started and ended:
            overlapping = (
                UsageSession.objects.filter(
                    equipment=self.instance.equipment, started_at__lt=ended, ended_at__gt=started
                )
                .exclude(pk=self.instance.pk)
                .exists()
            )
            if overlapping:
                self.add_error("started_at", "That overlaps another session on this machine.")
        return cleaned


@admin.register(UsageSession)
class UsageSessionAdmin(admin.ModelAdmin):
    form = UsageSessionForm
    list_display = ("started_at", "equipment", "member", "duration_display", "reason", "fee_pence", "ended_by")
    list_filter = ("equipment", "reason", "removed", "ended_by")
    search_fields = ("member__email", "member__given_name", "member__family_name")
    date_hierarchy = "started_at"
    autocomplete_fields = ("member",)
    readonly_fields = (
        "fee_pence",
        "charged_at",
        "original_started_at",
        "original_ended_at",
        "adjusted_by",
        "motor_on_seconds",
        "legacy_id",
    )

    @admin.display(description="Duration")
    def duration_display(self, session):
        minutes = session.duration_seconds // 60
        return f"{minutes // 60}h {minutes % 60:02d}m" + ("" if session.ended_at else " (open)")

    def save_model(self, request, obj, form, change):
        if change and {"started_at", "ended_at"} & set(form.changed_data):
            original = UsageSession.objects.get(pk=obj.pk)
            if obj.original_started_at is None:  # keep the very first times, even after several adjustments
                obj.original_started_at = original.started_at
                obj.original_ended_at = original.ended_at
            obj.adjusted_by = request.user
            if obj.ended_at and original.ended_at is None:
                obj.ended_by = UsageSession.EndedBy.ADMIN
            obj.removed = obj.ended_at is not None and (obj.ended_at - obj.started_at) <= rules.SHORT_SESSION
            if obj.ended_at:
                obj.fee_pence = rules.session_fee_pence(obj.equipment, obj)
            # TODO(payments app): if already charged, add a correcting ledger entry; notify the member.
        elif change and "reason" in form.changed_data and obj.ended_at:
            obj.fee_pence = rules.session_fee_pence(obj.equipment, obj)
        super().save_model(request, obj, form, change)
