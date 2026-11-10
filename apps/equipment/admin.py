from django import forms
from django.contrib import admin, messages
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from apps.payments import services

from . import lifetime, notices, photos, rules
from .models import (
    Equipment,
    EquipmentPhoto,
    Induction,
    MachineStatusChange,
    ServiceRecord,
    TrainingCompletion,
    UsageSession,
)


class _StoredPhoto(forms.Widget):
    """In place of the file chooser on a photo already stored, which can only be moved or deleted."""

    needs_multipart_form = True  # the admin checks the first row's widgets to decide whether files can be sent

    def render(self, name, value, attrs=None, renderer=None):
        return "Stored. To replace it, delete it and add another."

    def value_from_datadict(self, data, files, name):
        return None


class EquipmentPhotoForm(forms.ModelForm):
    """A photo is added by uploading a file, which is cropped to a square (05 §5.7); after that only its position
    changes. To replace one, delete it and add another."""

    upload = forms.FileField(
        label="Photo",
        required=False,
        help_text="JPEG, PNG or WebP. It's cropped to a square from the middle, so centre the machine.",
        # On a phone, "Take a photo of it" opens the rear camera (static/js/camera.js).
        widget=forms.ClearableFileInput(
            attrs={"accept": "image/jpeg,image/png,image/webp", "data-camera": "environment"}
        ),
    )

    class Meta:
        model = EquipmentPhoto
        fields = ("position",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk is not None:
            self.fields["upload"].widget = _StoredPhoto()

    def clean(self):
        cleaned = super().clean()
        upload = cleaned.get("upload")
        if self.instance.pk is None and not upload:
            if self.has_changed():
                self.add_error("upload", "Choose a photo to upload.")
            return cleaned
        if upload and self.instance.pk is None:
            try:
                self._images = photos.process(upload)
            except photos.BadPhoto as error:
                self.add_error("upload", str(error))
        return cleaned

    def save(self, commit=True):
        if getattr(self, "_images", None):
            photos.fill(self.instance, *self._images)
        return super().save(commit)


class EquipmentPhotoInline(admin.TabularInline):
    model = EquipmentPhoto
    form = EquipmentPhotoForm
    extra = 1
    fields = ("preview", "upload", "position")
    readonly_fields = ("preview",)

    class Media:
        css = {"all": ["css/camera.css"]}
        js = ["js/camera.js"]

    @admin.display(description="")
    def preview(self, photo):
        if not photo.pk:
            return ""
        url = reverse("equipment:photo", args=[photo.equipment.slug, photo.pk, "thumb"])
        return format_html('<img src="{}" alt="" width="100" height="100">', url)


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
    actions = ["retire"]
    list_filter = ("status", "requires_induction", "archived", "team")
    search_fields = ("name", "slug", "asset_tag_id")
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ("permaloan_member",)
    inlines = [EquipmentPhotoInline, ServiceRecordInline, MachineStatusChangeInline]
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
                    "purchase_price_pence",
                    "obtained_at",
                    "archived",
                    "removed_at",
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

    def changelist_view(self, request, extra_context=None):
        extra_context = {**(extra_context or {}), "retired_url": reverse("equipment:retired")}
        return super().changelist_view(request, extra_context)

    @admin.action(description="Retire the selected equipment (disposed of): keeps its history for review")
    def retire(self, request, queryset):
        today = timezone.localdate()
        count = 0
        for machine in queryset.filter(archived=False):
            machine.archived = True
            machine.removed_at = machine.removed_at or today
            machine.save(update_fields=["archived", "removed_at"])
            count += 1
        self.message_user(
            request,
            format_html(
                'Retired {} machine(s). <a href="{}">Review retired equipment</a>.', count, reverse("equipment:retired")
            ),
        )

    def has_delete_permission(self, request, obj=None):
        # Removing equipment means retiring it: deleting would lose its sessions, training and money (05 §5.7).
        # Only something added by mistake, with no history, can be deleted.
        if obj is not None and lifetime.has_history(obj):
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)  # it would skip the history check above
        return actions

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


class InductionForm(forms.ModelForm):
    class Meta:
        model = Induction
        fields = "__all__"  # noqa: DJ007 - the admin shows them all

    def clean(self):
        cleaned = super().clean()
        trained_at, member = cleaned.get("trained_at"), cleaned.get("member")
        if trained_at is None or member is None or "trained_at" not in self.changed_data:
            return cleaned
        # A confirmed email address, and the online course where a machine has one (05 §5.8, 03 §3.1, 06 §6.12).
        category = cleaned.get("category") or self.instance.category
        if problem := rules.sign_off_problem(member, category, trained_at):
            hint = (
                "(Members' emails can be confirmed or reinstated by an admin on their page.)"
                if not member.email_verified
                else 'Their course is checked when this list is opened with the "Waiting for training" filter.'
            )
            raise forms.ValidationError(f"{problem} {hint}")
        return cleaned


class WaitingFilter(admin.SimpleListFilter):
    """The sign-off list (05 §5.8): paid for, not yet signed off. Opening it asks Moodle about the members waiting
    for a course (06 §6.12), so the course status below is up to date."""

    title = "sign-off"
    parameter_name = "waiting"

    def lookups(self, request, model_admin):
        return [("yes", "Waiting for training")]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(paid=True, trained_at__isnull=True)
        return queryset


@admin.register(Induction)
class InductionAdmin(admin.ModelAdmin):
    form = InductionForm
    list_display = ("member", "category", "paid", "trained_at", "trainer", "is_trainer", "course")
    list_filter = (WaitingFilter, "category", "paid", "is_trainer")
    search_fields = ("member__email", "member__given_name", "member__family_name", "category")
    autocomplete_fields = ("member", "trainer")

    @admin.display(description="Online course")
    def course(self, induction):
        machines = [
            m for m in Equipment.objects.filter(moodle_course_id__isnull=False) if m.induction_key == induction.category
        ]
        if not machines:
            return "-"
        finished = TrainingCompletion.objects.filter(
            member=induction.member, moodle_course_id=machines[0].moodle_course_id, cleared_at__isnull=True
        ).exists()
        return "Finished" if finished else "Not finished"

    def changelist_view(self, request, extra_context=None):
        if request.GET.get("waiting") == "yes" and request.method == "GET":
            from apps.moodle import client, completion  # not at the top: apps.moodle uses this app's models

            if client.enabled():
                summary = completion.check_waiting(limit=25)
                if summary.unavailable:
                    messages.warning(request, "Moodle couldn't be reached, so the course status may be out of date.")
        return super().changelist_view(request, extra_context)


@admin.register(TrainingCompletion)
class TrainingCompletionAdmin(admin.ModelAdmin):
    """Online courses members have finished, as BBMS last checked Moodle (06 §6.12). A completion stays until an admin
    clears it, for example when someone must retake the course."""

    list_display = ("member", "moodle_course_id", "completed_at", "last_checked_at", "cleared_at")
    list_filter = ("moodle_course_id",)
    search_fields = ("member__email", "member__given_name", "member__family_name")
    autocomplete_fields = ("member",)
    readonly_fields = ("cleared_at", "cleared_by")
    actions = ["clear"]

    @admin.action(description="Clear the selected completions (they must finish the course again)")
    def clear(self, request, queryset):
        cleared = queryset.filter(cleared_at__isnull=True).update(cleared_at=timezone.now(), cleared_by=request.user)
        self.message_user(request, f"Cleared {cleared} completion(s).")


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
        times_before = None
        if change and {"started_at", "ended_at"} & set(form.changed_data):
            original = UsageSession.objects.get(pk=obj.pk)
            times_before = (original.started_at, original.ended_at)
            if obj.original_started_at is None:  # keep the very first times, even after several adjustments
                obj.original_started_at = original.started_at
                obj.original_ended_at = original.ended_at
            obj.adjusted_by = request.user
            if obj.ended_at and original.ended_at is None:
                obj.ended_by = UsageSession.EndedBy.ADMIN
            obj.removed = obj.ended_at is not None and (obj.ended_at - obj.started_at) <= rules.SHORT_SESSION
            if obj.ended_at:
                obj.fee_pence = rules.session_fee_pence(obj.equipment, obj)
        elif change and "reason" in form.changed_data and obj.ended_at:
            obj.fee_pence = rules.session_fee_pence(obj.equipment, obj)
        super().save_model(request, obj, form, change)
        if obj.ended_at:
            # Charge, or refund, the difference as a new ledger row (05 §5.6).
            correction = services.settle_session(obj)
            if times_before is not None:
                notices.session_adjusted(obj, times_before, correction)
            if correction is not None and correction.corrects_id:
                verb = "refunded" if correction.amount_pence < 0 else "charged"
                messages.info(request, f"The member was {verb} £{abs(correction.amount_pence) / 100:.2f}.")
            elif obj.legacy_id and correction is None and "ended_at" in form.changed_data:
                messages.warning(request, "This session was billed by the legacy system, so no money was moved.")
