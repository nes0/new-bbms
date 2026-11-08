"""An admin action on members: put their Discord roles right now (06 §6.14)."""

from django.contrib import admin, messages

from apps.members.admin import MemberAdmin

from . import client, linking


@admin.action(description="Update their Discord roles now")
def sync_discord(modeladmin, request, queryset):
    done, failed = 0, 0
    for member in queryset.exclude(discord_user_id=""):
        try:
            linking.sync(member)
            done += 1
        except client.DiscordError as error:
            failed += 1
            messages.warning(request, f"{member}: {error}")
    messages.info(request, f"Updated {done} linked member{'s' if done != 1 else ''} on Discord.")


# The member's admin page belongs to the members app; Discord adds its action to it.
MemberAdmin.actions = [*(MemberAdmin.actions or []), sync_discord]
