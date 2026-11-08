"""
Members and the space's Discord server (spec 06 §6.14).

- A new member **links their Discord account** before they can set up their payment (so before they can become
  active): BBMS adds them to the server if they aren't on it already. Trustees can excuse someone ("Discord not
  needed" on the member's admin page).
- BBMS keeps two roles right: the **member** role while someone is a member (active, leaving, suspended or
  honorary), and the **former member** role once they've left (it opens the left-members channel). It changes them
  when a member's status changes, and the nightly `sync_discord` job puts right anything else (an admin's edit, a
  role changed by hand on Discord, a missed call).
- Their **server nickname** is set to their real name when they link (so members know who's who). After that it's
  theirs to change: some members are known by another name at the space.
- When BBMS adds someone to the server it posts a **welcome message** for them in the welcome channel (its text is
  in Settings → Discord); the nightly job tries again if Discord was down.
- Topic channels (#laser, #kiln, ...) are chosen by members on Discord itself; BBMS only links to them.
"""

import logging

from django.db import transaction
from django.utils import timezone

from apps.members.models import Member
from apps.site_settings.models import SiteSettings

from . import client as discord

logger = logging.getLogger(__name__)

MEMBER_STATUSES = (Member.Status.ACTIVE, Member.Status.LEAVING, Member.Status.SUSPENDED, Member.Status.HONORARY)


class AlreadyLinked(discord.DiscordError):
    pass


def needed(member) -> bool:
    """Whether the member still has to link Discord (and be on the server)."""
    if not discord.enabled() or member.discord_exempt:
        return False
    return not (member.discord_user_id and member.discord_on_server)


def blocks_setup(member) -> bool:
    """A member who is joining can't set up their payment, so can't become active, until they've linked Discord."""
    return member.status == Member.Status.SETTING_UP and needed(member)


def wanted_roles(member) -> tuple[set[str], set[str]]:
    """(roles they should have, roles they shouldn't), of the ones BBMS manages."""
    member_role, former_role = discord.member_role(), discord.former_member_role()
    if member.status in MEMBER_STATUSES and not member.banned:
        give, take = {member_role}, {former_role}
    elif member.status == Member.Status.LEFT:
        give, take = {former_role}, {member_role}
    else:  # still setting up (or banned): neither, though a former member keeps that role until they're back
        give, take = set(), {member_role}
    return give - {""}, take - {""}


NICK_LENGTH = 32  # Discord's limit


def nickname_for(member) -> str:
    """Their real name; a long one is cut to the first name and initial (e.g. "Alexandra B.")."""
    full = " ".join(f"{member.given_name} {member.family_name}".split())
    if len(full) <= NICK_LENGTH:
        return full
    short = f"{member.given_name.strip()} {member.family_name.strip()[:1]}.".strip()
    return short[:NICK_LENGTH]


def welcome_text(member) -> str:
    config = SiteSettings.load()
    replacements = {
        "{mention}": f"<@{member.discord_user_id}>",
        "{name}": member.full_name,
        "{first_name}": member.given_name,
        "{space}": config.space_name,
    }
    text = config.discord_welcome_message
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)
    return text.strip()[:2000]


def post_welcome(member) -> bool:
    """Post the member's welcome message, if one is due. Returns True if it was posted (or isn't wanted)."""
    if not member.discord_welcome_due:
        return True
    channel, text = discord.welcome_channel(), welcome_text(member)
    if channel and text:
        try:
            discord.client().post(channel, text, member.discord_user_id)
        except discord.DiscordError as error:
            logger.warning("Member %s: welcome message not posted yet: %s", member.pk, error)
            return False
    member.discord_welcome_due = False
    Member.objects.filter(pk=member.pk).update(discord_welcome_due=False)
    return True


def link(member, code, redirect_uri) -> bool:
    """Finish linking: who they are on Discord, then onto the server with the right roles. Returns True if BBMS added
    them to the server (False if they were already on it). Raises DiscordError (AlreadyLinked for an account another
    member has linked).
    """
    api = discord.client()
    token = api.exchange_code(code, redirect_uri)
    user = api.me(token)
    if Member.objects.filter(discord_user_id=user.id).exclude(pk=member.pk).exists():
        raise AlreadyLinked("That Discord account is linked to another member's account. Please ask a trustee.")
    give, _ = wanted_roles(member)
    added = api.add_to_server(user.id, token, give, nickname_for(member))
    member.discord_user_id = user.id
    member.discord_username = user.username[:100]
    member.discord_linked_at = timezone.now()
    member.discord_on_server = True
    member.discord_welcome_due = added
    member.save(
        update_fields=[
            "discord_user_id",
            "discord_username",
            "discord_linked_at",
            "discord_on_server",
            "discord_welcome_due",
        ]
    )
    logger.info("Member %s linked a Discord account (%s)", member.pk, "added to the server" if added else "on it")
    if added:
        post_welcome(member)
    else:
        _set_nick(member)  # already on the server: their real name, this once
        sync(member)  # and the right roles
    return added


def _set_nick(member):
    try:
        discord.client().set_nick(member.discord_user_id, nickname_for(member))
    except discord.DiscordError as error:  # e.g. the server's owner, whom no bot may rename
        logger.warning("Member %s: nickname not set: %s", member.pk, error)


def unlink(member):
    """Forget the member's Discord account (their roles on Discord are left as they are)."""
    member.discord_user_id = member.discord_username = ""
    member.discord_linked_at = None
    member.discord_on_server = member.discord_welcome_due = False
    member.save(
        update_fields=[
            "discord_user_id",
            "discord_username",
            "discord_linked_at",
            "discord_on_server",
            "discord_welcome_due",
        ]
    )


def sync(member) -> str:
    """Put right the member and former-member roles, and post a welcome that's due. Returns what happened. Raises
    DiscordError. (The nickname is only set when they link.)
    """
    if not member.discord_user_id:
        return "not linked"
    api = discord.client()
    try:
        found = api.server_member(member.discord_user_id)
    except discord.NotOnServer:
        if member.discord_on_server:
            Member.objects.filter(pk=member.pk).update(discord_on_server=False)
        return "not on the server"
    give, take = wanted_roles(member)
    changes = []
    for role in give - found.roles:
        api.add_role(member.discord_user_id, role)
        changes.append(f"+{role}")
    for role in take & found.roles:
        api.remove_role(member.discord_user_id, role)
        changes.append(f"-{role}")
    if member.discord_welcome_due and post_welcome(member):
        changes.append("welcomed")
    if not member.discord_on_server:
        Member.objects.filter(pk=member.pk).update(discord_on_server=True)
    return ", ".join(changes) or "no change"


def sync_soon(member):
    """After a status change: put their roles right once it's saved. Failures are logged; the nightly job retries."""
    if not member.discord_user_id or not discord.enabled():
        return
    member_id = member.pk

    def run():
        try:
            sync(Member.objects.get(pk=member_id))
        except (discord.DiscordError, Member.DoesNotExist) as error:
            logger.warning("Member %s: Discord roles not updated: %s", member_id, error)

    transaction.on_commit(run)


def sync_all() -> dict:
    """The nightly check of everyone with a linked account."""
    results = {"changed": 0, "unchanged": 0, "not on the server": 0, "failed": 0}
    for member in Member.objects.exclude(discord_user_id="").order_by("pk"):
        try:
            outcome = sync(member)
        except discord.DiscordError as error:
            logger.warning("Member %s: Discord sync failed: %s", member.pk, error)
            results["failed"] += 1
            continue
        if outcome == "not on the server":
            results["not on the server"] += 1
        elif outcome == "no change":
            results["unchanged"] += 1
        else:
            results["changed"] += 1
    return results
