"""
Photos waiting for approval, profiles on the member's admin page, and what the groups page says about each role
(spec 06 §6.1, 03 §3.9).
"""

from django.contrib import admin, messages
from django.contrib.auth.admin import GroupAdmin
from django.contrib.auth.models import Group
from django.db import models
from django.urls import reverse
from django.utils.html import format_html

from apps.members.admin import MemberAdmin
from apps.members.models import MemberPhoto

from . import photos
from .models import DiscordChannel, Profile, RoleInfo


def _thumb(photo):
    url = reverse("profiles:photo", args=[photo.member_id, "thumb"])
    if photo.kind == MemberPhoto.Kind.PENDING:
        url += "?waiting=1"
    return format_html('<img src="{}" width="120" height="120" alt="" style="border-radius:6px">', url)


@admin.register(MemberPhoto)
class MemberPhotoAdmin(admin.ModelAdmin):
    """The photo queue: approve or decline what members upload. Declining emails the member."""

    list_display = ("preview", "member", "kind", "uploaded_at", "current_photo")
    list_filter = ("kind",)
    search_fields = ("member__email", "member__given_name", "member__family_name")
    list_select_related = ("member",)
    actions = ["approve", "decline", "remove"]
    fields = ("member", "kind", "uploaded_at", "preview")
    readonly_fields = fields

    @admin.display(description="Photo")
    def preview(self, photo):
        return _thumb(photo)

    @admin.display(description="Photo now")
    def current_photo(self, photo):
        if photo.kind != MemberPhoto.Kind.PENDING:
            return ""
        current = MemberPhoto.objects.filter(member_id=photo.member_id, kind=MemberPhoto.Kind.CURRENT).first()
        return _thumb(current) if current else "None"

    def has_add_permission(self, request):
        return False  # members upload from their profile

    def has_delete_permission(self, request, obj=None):
        return False  # use "Delete" below, which also clears "photo approved"

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.has_perm("members.change_memberphoto"):
            return {}
        return actions

    @admin.action(description="Approve (replaces their photo)")
    def approve(self, request, queryset):
        done = 0
        for photo in queryset.filter(kind=MemberPhoto.Kind.PENDING).select_related("member"):
            photos.approve(photo, request.user)
            done += 1
        messages.success(request, f"{done} photo{'s' if done != 1 else ''} approved.")

    @admin.action(description="Decline (emails the member to send another)")
    def decline(self, request, queryset):
        done = 0
        for photo in queryset.filter(kind=MemberPhoto.Kind.PENDING).select_related("member"):
            photos.decline(photo, request.user)
            done += 1
        messages.success(request, f"{done} photo{'s' if done != 1 else ''} declined; the members have been emailed.")

    @admin.action(
        description="Delete (an approved photo: the member then has none, so a key holder's fob won't open the door)"
    )
    def remove(self, request, queryset):
        done = 0
        for photo in queryset.select_related("member"):
            photos.remove(photo.member, photo.kind, request.user)
            done += 1
        messages.success(request, f"{done} photo{'s' if done != 1 else ''} deleted.")

    def changelist_view(self, request, extra_context=None):
        # Open on the queue: photos waiting for approval.
        if "kind__exact" not in request.GET and not request.GET.get("q") and request.method == "GET":
            query = request.GET.copy()
            query["kind__exact"] = MemberPhoto.Kind.PENDING
            request.GET = query
            request.META["QUERY_STRING"] = query.urlencode()
        queue = request.GET.get("kind__exact") == MemberPhoto.Kind.PENDING and not request.GET.get("q")
        if queue and request.method == "GET" and not MemberPhoto.objects.filter(kind=MemberPhoto.Kind.PENDING).exists():
            # Usually because someone followed a notification after another admin had already dealt with the photo.
            messages.info(request, "No photos are waiting for approval: another admin may already have dealt with it.")
        return super().changelist_view(request, extra_context)


class ProfileInline(admin.StackedInline):
    model = Profile
    formfield_overrides = {models.URLField: {"assume_scheme": "https"}}
    can_delete = False
    extra = 0
    classes = ("collapse",)
    fields = ("tagline", "description", "skills", "website", "github", "twitter", "facebook", "irc", "photo_private")


class PhotoInline(admin.TabularInline):
    model = MemberPhoto
    extra = 0
    can_delete = False  # Members → Member photos: "Delete" also clears "photo approved"
    fields = ("preview", "kind", "uploaded_at")
    readonly_fields = fields
    verbose_name_plural = "Photos (approve waiting ones from Members → Member photos)"

    @admin.display(description="Photo")
    def preview(self, photo):
        return _thumb(photo) if photo.pk else ""

    def has_add_permission(self, request, obj=None):
        return False


# The member's admin page belongs to the members app; profiles add their parts to it.
MemberAdmin.inlines = [*MemberAdmin.inlines, PhotoInline, ProfileInline]


class RoleInfoInline(admin.StackedInline):
    model = RoleInfo
    can_delete = False
    verbose_name_plural = "On the groups page"


class DiscordChannelInline(admin.TabularInline):
    model = DiscordChannel
    extra = 1
    fields = ("name", "channel_id", "order")
    verbose_name_plural = "Discord channels (a group can have several)"


admin.site.unregister(Group)


@admin.register(Group)
class RoleAdmin(GroupAdmin):
    inlines = [RoleInfoInline, DiscordChannelInline]
    list_display = ("name", "title", "listed")

    @admin.display(description="Title")
    def title(self, group):
        info = getattr(group, "info", None)
        return info.title if info else ""

    @admin.display(description="On the groups page", boolean=True)
    def listed(self, group):
        info = getattr(group, "info", None)
        return info.listed if info else True
