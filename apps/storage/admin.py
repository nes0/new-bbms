"""
Lockers in the admin: the storage team's and trustees' side (spec 06 §6.2).

Prices, availability, rentals and members' limits are changed through apps/storage/rules.py, so every change is
checked and logged. Who can do what is ordinary Django permissions: give the storage role the permissions it needs.
"""

from decimal import Decimal

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from apps.members.admin import MemberAdmin
from apps.payments.forms import PoundsField, to_pence
from apps.payments.templatetags.money import pounds

from . import rules
from .models import Locker, LockerChange, LockerRent, LockerRental, LockerSize, MemberLockerLimit, StorageLocation


def _pounds_initial(pence):
    return None if pence is None else (Decimal(pence) / 100).quantize(Decimal("0.01"))


def _optional_pounds(**kwargs):
    return PoundsField(required=False, min_value=Decimal("0"), **kwargs)


# --- places, sizes and default rents ---


@admin.register(StorageLocation)
class StorageLocationAdmin(admin.ModelAdmin):
    list_display = ("name", "description", "sort_order")
    list_editable = ("sort_order",)


@admin.register(LockerSize)
class LockerSizeAdmin(admin.ModelAdmin):
    list_display = ("name", "description", "sort_order")
    list_editable = ("sort_order",)


class LockerRentForm(forms.ModelForm):
    monthly_rent = PoundsField(label="Default monthly rent (£)", min_value=Decimal("0"))
    reason = forms.CharField(required=False, help_text="Why it's changing (kept in the log).")

    class Meta:
        model = LockerRent
        fields = ["location", "size"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["monthly_rent"].initial = _pounds_initial(self.instance.monthly_rent_pence)

    def save(self, commit=True):
        self.instance.monthly_rent_pence = to_pence(self.cleaned_data["monthly_rent"])
        return super().save(commit)


@admin.register(LockerRent)
class LockerRentAdmin(admin.ModelAdmin):
    """The default rent for each size of locker in each location."""

    form = LockerRentForm
    list_display = ("location", "size", "rent_display", "lockers")
    list_filter = ("location", "size")
    fields = ("location", "size", "monthly_rent", "reason")

    @admin.display(description="Default monthly rent", ordering="monthly_rent_pence")
    def rent_display(self, rent):
        return pounds(rent.monthly_rent_pence)

    @admin.display(description="Lockers")
    def lockers(self, rent):
        return Locker.objects.filter(location=rent.location_id, size=rent.size_id, retired=False).count()

    def save_model(self, request, obj, form, change):
        old = LockerRent.objects.filter(pk=obj.pk).values_list("monthly_rent_pence", flat=True).first()
        super().save_model(request, obj, form, change)
        if old != obj.monthly_rent_pence:
            counts = rules.default_rent_changed(obj, old, request.user, form.cleaned_data.get("reason", ""))
            if counts["raised"]:
                messages.warning(
                    request,
                    f"{counts['raised']} member(s) renting these lockers have been emailed: they pay the new rent "
                    f"from {rules.NOTICE_DAYS} days from today.",
                )
            if counts["lowered"]:
                messages.info(request, f"{counts['lowered']} member(s) renting these lockers pay the lower rent now.")


# --- lockers ---


class StatusFilter(admin.SimpleListFilter):
    title = "status"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return [
            ("rented", "Rented"),
            ("available", "Available"),
            (Locker.Hold.RESERVED, "Reserved for trustees"),
            (Locker.Hold.OUT_OF_USE, "Out of use"),
            ("left", "Rented by someone who has left"),
        ]

    def queryset(self, request, queryset):
        rented = LockerRental.objects.filter(ended_at__isnull=True)
        value = self.value()
        if value == "rented":
            return queryset.filter(pk__in=rented.values("locker"))
        if value == "available":
            return queryset.filter(hold="", retired=False).exclude(pk__in=rented.values("locker"))
        if value in (Locker.Hold.RESERVED, Locker.Hold.OUT_OF_USE):
            return queryset.filter(hold=value).exclude(pk__in=rented.values("locker"))
        if value == "left":
            return queryset.filter(pk__in=rented.filter(member__status="left").values("locker"))
        return queryset


class LockerForm(forms.ModelForm):
    """The locker itself, plus its availability and advertised price while it's empty (changed through the rules)."""

    advertised_rent = _optional_pounds(
        label="Advertised rent override (£)",
        help_text="For an empty locker: the price shown, and paid by whoever rents it next. Empty = the default.",
    )
    change_reason = forms.CharField(
        required=False, label="Reason", help_text="Needed for a price override; kept in the log."
    )

    class Meta:
        model = Locker
        fields = ["location", "number", "size", "hold", "hold_note", "retired"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["advertised_rent"].initial = _pounds_initial(self.instance.advertised_rent_pence)
        self.fields["change_reason"].initial = self.instance.advertised_rent_reason
        self.rented = self.instance.pk is not None and rules.open_rental(self.instance) is not None
        if self.rented:
            for name in ("hold", "hold_note", "advertised_rent", "retired"):
                self.fields[name].disabled = True
                self.fields[name].help_text = "It's rented: end the rental first, or change the rent on the rental."

    def clean(self):
        cleaned = super().clean()
        if self.rented:
            return cleaned
        if cleaned.get("hold") and not cleaned.get("hold_note"):
            self.add_error("hold_note", "Say what it's reserved for, or what's wrong with it.")
        advertised = cleaned.get("advertised_rent")
        if advertised is not None and to_pence(advertised) != self.instance.advertised_rent_pence:
            if not cleaned.get("change_reason"):
                self.add_error("change_reason", "Give a reason for the price override.")
        location, size = cleaned.get("location"), cleaned.get("size")
        if location and size and advertised is None and not LockerRent.objects.filter(location=location, size=size):
            self.add_error(
                "size", f"Set the default rent for {size} lockers in {location} first (Storage → Default rents)."
            )
        return cleaned


class LockerRentalInline(admin.TabularInline):
    model = LockerRental
    extra = 0
    can_delete = False
    fields = ("member", "started_at", "ended_at", "rent_display", "end_reason")
    readonly_fields = fields
    show_change_link = True
    verbose_name_plural = "Rentals"

    @admin.display(description="Monthly rent")
    def rent_display(self, rental):
        return _rent_label(rental)

    def has_add_permission(self, request, obj=None):
        return False


def _rent_label(rental):
    text = pounds(rules.rental_rent_pence(rental))
    if rental.rent_override_pence is not None:
        return f"{text} (override)"
    upcoming = rules.upcoming_rent(rental)
    return f"{text} ({pounds(upcoming[0])} from {upcoming[1]:%d %b %Y})" if upcoming else text


@admin.register(Locker)
class LockerAdmin(admin.ModelAdmin):
    form = LockerForm
    list_display = ("__str__", "size", "status_display", "holder", "rent_display", "since")
    list_filter = (StatusFilter, "location", "size", "retired")
    search_fields = ("number", "location__name", "rentals__member__given_name", "rentals__member__family_name")
    list_select_related = ("location", "size")
    inlines = [LockerRentalInline]
    fieldsets = (
        (None, {"fields": ("location", "number", "size", "retired")}),
        ("While it's empty", {"fields": ("hold", "hold_note", "advertised_rent", "change_reason")}),
    )

    @admin.display(description="Status")
    def status_display(self, locker):
        state = rules.status(locker)
        return {
            rules.RENTED: "Rented",
            rules.AVAILABLE: "Available",
            rules.NO_PRICE: "No price set",
            Locker.Hold.RESERVED: f"Reserved: {locker.hold_note}",
            Locker.Hold.OUT_OF_USE: f"Out of use: {locker.hold_note}",
        }[state]

    @admin.display(description="Rented by")
    def holder(self, locker):
        rental = rules.open_rental(locker)
        if rental is None:
            return "—"
        return f"{rental.member} (left)" if rental.member.status == "left" else str(rental.member)

    @admin.display(description="Monthly rent")
    def rent_display(self, locker):
        rental = rules.open_rental(locker)
        if rental is not None:
            return _rent_label(rental)
        price = rules.advertised_rent_pence(locker)
        return f"{pounds(price)} (override)" if locker.advertised_rent_pence is not None else pounds(price)

    @admin.display(description="Since")
    def since(self, locker):
        rental = rules.open_rental(locker)
        return rental.started_at.date() if rental else "—"

    def save_model(self, request, obj, form, change):
        if form.rented:
            super().save_model(request, obj, form, change)  # only location, number and size can change
            return
        # Availability and the advertised price go through the rules (checked and logged): save the locker with its
        # stored values first, then apply the changes.
        stored = Locker.objects.filter(pk=obj.pk).first() if change else None
        hold, note = obj.hold, obj.hold_note
        obj.hold, obj.hold_note = (stored.hold, stored.hold_note) if stored else ("", "")
        super().save_model(request, obj, form, change)
        reason = form.cleaned_data.get("change_reason", "")
        if (hold, note) != (obj.hold, obj.hold_note):
            rules.set_availability(obj, hold, note, request.user)
            obj.refresh_from_db()
        advertised = form.cleaned_data.get("advertised_rent")
        pence = to_pence(advertised) if advertised is not None else None
        if pence != obj.advertised_rent_pence:
            rules.set_advertised_rent(obj, pence, reason, request.user)

    def has_delete_permission(self, request, obj=None):
        # A locker with any rental history is retired instead, so the history is kept.
        return obj is None or not obj.rentals.exists()


# --- rentals ---


class StartRentalForm(forms.ModelForm):
    class Meta:
        model = LockerRental
        fields = ["locker", "member"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["locker"].queryset = Locker.objects.filter(
            pk__in=[locker.pk for locker, _ in rules.available_lockers()]
        )
        self.fields["locker"].help_text = "Available lockers only. An advertised price override becomes this rental's."

    def clean(self):
        cleaned = super().clean()
        # Check the rules now, so problems show on the form rather than as an error page.
        member, locker = cleaned.get("member"), cleaned.get("locker")
        if member and locker:
            held = rules.open_rentals(member).count()
            if not member.has_space_access:
                raise ValidationError(f"{member} isn't a current member.")
            if held >= rules.locker_limit(member):
                raise ValidationError(f"{member} already has {held} locker(s), their limit.")
        return cleaned


class RentalForm(forms.ModelForm):
    rent_override = _optional_pounds(
        label="Rent override (£)", help_text="What this member pays instead of the default. Empty = the default."
    )
    reason = forms.CharField(required=False, help_text="Needed when the rent changes; kept in the log.")
    end_now = forms.BooleanField(required=False, label="End this rental now (the locker is given up)")
    end_reason = forms.CharField(required=False, label="Why it ended", max_length=255)

    class Meta:
        model = LockerRental
        fields = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rent_override"].initial = _pounds_initial(self.instance.rent_override_pence)
        self.fields["reason"].initial = self.instance.rent_override_reason
        if not self.instance.is_open:
            for field in self.fields.values():
                field.disabled = True

    def clean(self):
        cleaned = super().clean()
        new = cleaned.get("rent_override")
        pence = to_pence(new) if new is not None else None
        if pence != self.instance.rent_override_pence and not cleaned.get("reason"):
            self.add_error("reason", "Give a reason for changing what this member pays.")
        cleaned["rent_override_pence"] = pence
        return cleaned


@admin.register(LockerRental)
class LockerRentalAdmin(admin.ModelAdmin):
    list_display = ("locker", "member", "rent_display", "started_at", "ended_at")
    list_filter = (("ended_at", admin.EmptyFieldListFilter), "locker__location")
    search_fields = ("member__email", "member__given_name", "member__family_name", "locker__location__name")
    list_select_related = ("locker__location", "member")
    readonly_fields = ("locker", "member", "started_at", "started_by", "default_rent", "ended_at", "ended_by")

    @admin.display(description="Monthly rent")
    def rent_display(self, rental):
        return _rent_label(rental)

    @admin.display(description="Default rent for this locker")
    def default_rent(self, rental):
        return pounds(rules.default_rent_pence(rental.locker))

    def get_form(self, request, obj=None, **kwargs):
        kwargs["form"] = StartRentalForm if obj is None else RentalForm
        return super().get_form(request, obj, **kwargs)

    def get_fields(self, request, obj=None):
        if obj is None:
            return ["locker", "member"]
        return [*self.readonly_fields, "rent_override", "reason", "end_now", "end_reason"]

    def get_readonly_fields(self, request, obj=None):
        return () if obj is None else self.readonly_fields

    def save_model(self, request, obj, form, change):
        try:
            if not change:
                rental = rules.start_rental(obj.locker, obj.member, request.user)
                obj.pk = rental.pk
                return
            # The form has no model fields, so `obj` still holds the stored values.
            new_rent = form.cleaned_data["rent_override_pence"]
            if new_rent != obj.rent_override_pence:
                rules.set_rental_rent(obj, new_rent, form.cleaned_data["reason"], request.user)
            if form.cleaned_data.get("end_now"):
                rules.end_rental(obj, request.user, form.cleaned_data.get("end_reason", ""))
        except rules.LockerError as error:
            messages.error(request, str(error))

    def has_delete_permission(self, request, obj=None):
        return False  # rentals are history; end them instead


# --- members' limits ---


class MemberLockerLimitForm(forms.ModelForm):
    class Meta:
        model = MemberLockerLimit
        fields = ["member", "limit", "reason"]


@admin.register(MemberLockerLimit)
class MemberLockerLimitAdmin(admin.ModelAdmin):
    """Members a trustee has allowed a different number of lockers. Delete one to go back to the usual limit."""

    form = MemberLockerLimitForm
    list_display = ("member", "limit", "reason", "set_by", "set_at")
    autocomplete_fields = ("member",)

    def save_model(self, request, obj, form, change):
        rules.set_member_limit(obj.member, obj.limit, obj.reason, request.user)

    def delete_model(self, request, obj):
        rules.set_member_limit(obj.member, None, "Back to the usual limit", request.user)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self.delete_model(request, obj)


class MemberLockerLimitInline(admin.StackedInline):
    """Shown on the member's admin page (06 §6.2). Changed from Storage → member's locker limits, which logs it."""

    model = MemberLockerLimit
    fk_name = "member"
    fields = ("limit", "reason", "set_by", "set_at")
    readonly_fields = fields
    can_delete = False
    max_num = 0

    def has_add_permission(self, request, obj=None):
        return False


MemberAdmin.inlines = [*MemberAdmin.inlines, MemberLockerLimitInline]


@admin.register(LockerChange)
class LockerChangeAdmin(admin.ModelAdmin):
    list_display = ("changed_at", "what", "description", "old_value", "new_value", "reason", "changed_by")
    list_filter = ("what",)
    search_fields = ("description", "reason")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
