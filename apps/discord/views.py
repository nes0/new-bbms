"""Linking a member's Discord account (spec 06 §6.14), and the fake Discord page used when developing."""

import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from . import client as discord
from . import linking

STATE_KEY = "discord_link_state"


def _callback(request):
    return request.build_absolute_uri(reverse("discord:done"))


@login_required
def page(request):
    member = request.user
    context = {
        "member": member,
        "enabled": discord.enabled(),
        "needed": linking.needed(member),
        "blocks_setup": linking.blocks_setup(member),
    }
    return render(request, "discord/link.html", context)


@login_required
@require_POST
def connect(request):
    """Send the member to Discord to say who they are and let BBMS add them to the server."""
    if not discord.enabled():
        messages.error(request, "Discord linking isn't available on this site at the moment.")
        return redirect("discord:page")
    state = secrets.token_urlsafe(24)
    request.session[STATE_KEY] = state
    return redirect(discord.client().authorize_url(_callback(request), state))


@login_required
def done(request):
    """Discord sends the member back here."""
    state = request.session.pop(STATE_KEY, None)
    if not state or request.GET.get("state") != state:
        messages.error(request, "That didn't work: please try linking Discord again.")
        return redirect("discord:page")
    if request.GET.get("error") or not request.GET.get("code"):
        messages.info(request, "Discord wasn't linked. You can try again whenever you're ready.")
        return redirect("discord:page")
    try:
        added = linking.link(request.user, request.GET["code"], _callback(request))
    except discord.DiscordError as error:
        messages.error(request, str(error))
        return redirect("discord:page")
    member = request.user
    member.refresh_from_db()
    joined = "We've added you to our server." if added else "You're already on our server."
    messages.success(request, f"Discord linked as {member.discord_username}. {joined}")
    return redirect("members:account")


@login_required
@require_POST
def unlink(request):
    linking.unlink(request.user)
    messages.success(request, "Discord unlinked. Link it again to use a different account.")
    return redirect("discord:page")


# --- the fake Discord page (local development only) ---


@require_http_methods(["GET", "POST"])
def fake_discord(request):
    """Stands in for Discord's "authorise" page when the DISCORD_* settings aren't set. DEBUG only."""
    if not settings.DEBUG or not discord.using_fake():
        raise Http404
    state = request.GET.get("state", "")
    if request.method == "POST":
        username = request.POST.get("username", "").strip() or "fake-user"
        code = discord.fake().log_in(username)
        return redirect(f"{reverse('discord:done')}?code={code}&state={state}")
    return render(request, "discord/fake_discord.html", {"state": state})
