"""
Talking to Discord (spec 06 §6.14).

Two kinds of call:
- **as the member**, with the short-lived token Discord gives BBMS when they link their account (OAuth2, scopes
  `identify` and `guilds.join`): who they are. The token is used once, to add them to the server, and not kept;
- **as the bot** (`DISCORD_BOT_TOKEN`): adding a member to the server, giving or taking away roles, setting their
  server nickname to their real name when they link, and posting the welcome message. The bot must be on the
  server with "Create Invite", "Manage Roles", "Manage Nicknames", and "Send Messages" in the welcome channel, and
  its own role must be above the roles it manages. (Discord never lets a bot change the server owner's nickname.)

`client()` returns one of:
- `DiscordApi`: the real API, when the settings are all there;
- `FakeDiscord`: when developing or testing without them. Its "Discord login page" is `/dev/discord/` (DEBUG only);
  when developing, its state is kept in a file so every process sees it;
- `NotConfigured`: a live site without them. Members aren't asked to link Discord (`enabled()` is False).
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlencode

from django.conf import settings

logger = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
AUTHORIZE = "https://discord.com/oauth2/authorize"
SCOPES = "identify guilds.join"
NEEDED = (
    "DISCORD_CLIENT_ID",
    "DISCORD_CLIENT_SECRET",
    "DISCORD_BOT_TOKEN",
    "DISCORD_GUILD_ID",
    "DISCORD_MEMBER_ROLE_ID",
)


class DiscordError(Exception):
    """Discord refused or couldn't be reached. The message is safe to show a member."""


class NotOnServer(DiscordError):
    """The account isn't on the server (it left, or was removed)."""


@dataclass
class User:
    id: str
    username: str


@dataclass
class ServerMember:
    roles: set
    nick: str


def configured() -> bool:
    return all(getattr(settings, name) for name in NEEDED)


def using_fake() -> bool:
    return not configured() and (settings.DEBUG or getattr(settings, "TESTING", False))


def enabled() -> bool:
    """Whether members are asked to link Discord at all."""
    return configured() or using_fake()


def member_role() -> str:
    return settings.DISCORD_MEMBER_ROLE_ID or FakeDiscord.MEMBER_ROLE


def welcome_channel() -> str:
    if configured():
        return settings.DISCORD_WELCOME_CHANNEL_ID
    return FakeDiscord.WELCOME_CHANNEL


def former_member_role() -> str:
    if configured():
        return settings.DISCORD_FORMER_MEMBER_ROLE_ID
    return FakeDiscord.FORMER_ROLE


class DiscordApi:
    """The real API, with plain HTTP calls (the few BBMS needs)."""

    def _call(self, method, path, *, token=None, bot=True, body=None, form=None, retry=True):
        headers = {"User-Agent": f"DiscordBot ({settings.SITE_URL}, 1)"}  # the form Discord asks for
        data = None
        if form is not None:
            data = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if bot:
            headers["Authorization"] = f"Bot {settings.DISCORD_BOT_TOKEN}"
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                text = response.read()
                return response.status, (json.loads(text) if text else None)
        except urllib.error.HTTPError as error:
            text = error.read()
            try:
                payload = json.loads(text) if text else {}
            except ValueError:
                payload = {}
            if error.code == 429 and retry:  # rate limited: wait as asked, once
                time.sleep(min(float(payload.get("retry_after", 1)), 10))
                return self._call(method, path, token=token, bot=bot, body=body, form=form, retry=False)
            if error.code == 404 and payload.get("code") == 10007:  # Unknown Member
                raise NotOnServer("That Discord account isn't on the server.") from error
            logger.warning("Discord said %s to %s %s: %s", error.code, method, path, payload.get("message", ""))
            raise DiscordError("Discord refused that. Please try again later.") from error
        except OSError as error:
            logger.warning("Couldn't reach Discord (%s %s): %s", method, path, error)
            raise DiscordError("We couldn't reach Discord. Please try again in a few minutes.") from error

    def authorize_url(self, redirect_uri, state) -> str:
        params = {
            "client_id": settings.DISCORD_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "prompt": "consent",
        }
        return f"{AUTHORIZE}?{urlencode(params)}"

    def exchange_code(self, code, redirect_uri) -> str:
        """The member's access token, from the code Discord sent them back with."""
        _, payload = self._call(
            "POST",
            "/oauth2/token",
            bot=False,
            form={
                "client_id": settings.DISCORD_CLIENT_ID,
                "client_secret": settings.DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        return payload["access_token"]

    def me(self, token) -> User:
        _, payload = self._call("GET", "/users/@me", bot=False, token=token)
        return User(payload["id"], payload.get("global_name") or payload["username"])

    def add_to_server(self, user_id, token, roles, nick) -> bool:
        """Adds them (with these roles and nickname) if they aren't on the server. Returns True if they were added."""
        status, _ = self._call(
            "PUT",
            f"/guilds/{settings.DISCORD_GUILD_ID}/members/{user_id}",
            body={"access_token": token, "roles": list(roles), "nick": nick},
        )
        return status == 201

    def server_member(self, user_id) -> ServerMember:
        """Raises NotOnServer."""
        _, payload = self._call("GET", f"/guilds/{settings.DISCORD_GUILD_ID}/members/{user_id}")
        return ServerMember(set(payload.get("roles", [])), payload.get("nick") or "")

    def set_nick(self, user_id, nick):
        self._call("PATCH", f"/guilds/{settings.DISCORD_GUILD_ID}/members/{user_id}", body={"nick": nick})

    def post(self, channel_id, content, mention_user_id):
        """A message from the bot. Only `mention_user_id` is notified: no @everyone or role pings from the text."""
        self._call(
            "POST",
            f"/channels/{channel_id}/messages",
            body={"content": content, "allowed_mentions": {"parse": [], "users": [mention_user_id]}},
        )

    def add_role(self, user_id, role_id):
        self._call("PUT", f"/guilds/{settings.DISCORD_GUILD_ID}/members/{user_id}/roles/{role_id}")

    def remove_role(self, user_id, role_id):
        self._call("DELETE", f"/guilds/{settings.DISCORD_GUILD_ID}/members/{user_id}/roles/{role_id}")


@dataclass
class FakeDiscord:
    """Behaves like Discord for local development and tests. Nothing leaves the machine."""

    MEMBER_ROLE, FORMER_ROLE = "role-member", "role-former"

    path: str | None = None
    server: dict = field(default_factory=dict)  # user id -> list of role ids, for everyone on the server
    nicks: dict = field(default_factory=dict)  # user id -> server nickname
    messages: list = field(default_factory=list)  # (channel id, content) the bot posted
    codes: dict = field(default_factory=dict)  # code -> user id
    users: dict = field(default_factory=dict)  # user id -> username
    next_id: int = 1
    fail_next: str = ""

    STATE = ("server", "nicks", "messages", "codes", "users", "next_id")
    WELCOME_CHANNEL = "channel-welcome"

    def load(self):
        if self.path and os.path.exists(self.path):
            with open(self.path) as file:
                for name, value in json.load(file).items():
                    setattr(self, name, value)
        return self

    def _save(self):
        if self.path:
            with open(self.path, "w") as file:
                json.dump({name: getattr(self, name) for name in self.STATE}, file, indent=1)

    def _maybe_fail(self):
        if self.fail_next:
            message, self.fail_next = self.fail_next, ""
            raise DiscordError(message)

    def authorize_url(self, redirect_uri, state) -> str:
        return f"/dev/discord/?{urlencode({'redirect_uri': redirect_uri, 'state': state})}"

    def log_in(self, username, user_id=None) -> str:
        """What the member does on Discord's page: returns the code Discord sends back."""
        if user_id is None:
            user_id = str(900000000000000000 + self.next_id)
            self.next_id += 1
        self.users[user_id] = username
        code = f"code-{user_id}-{len(self.codes)}"
        self.codes[code] = user_id
        self._save()
        return code

    def exchange_code(self, code, redirect_uri) -> str:
        self._maybe_fail()
        if code not in self.codes:
            raise DiscordError("Discord refused that. Please try again later.")
        return f"token-{self.codes[code]}"

    def me(self, token) -> User:
        user_id = token.removeprefix("token-")
        return User(user_id, self.users[user_id])

    def add_to_server(self, user_id, token, roles, nick) -> bool:
        self._maybe_fail()
        if user_id in self.server:
            return False
        self.server[user_id] = list(roles)
        self.nicks[user_id] = nick
        self._save()
        return True

    def server_member(self, user_id) -> ServerMember:
        self._maybe_fail()
        if user_id not in self.server:
            raise NotOnServer("That Discord account isn't on the server.")
        return ServerMember(set(self.server[user_id]), self.nicks.get(user_id, ""))

    def set_nick(self, user_id, nick):
        self._maybe_fail()
        self.nicks[user_id] = nick
        self._save()

    def post(self, channel_id, content, mention_user_id):
        self._maybe_fail()
        self.messages.append([channel_id, content])
        self._save()

    def add_role(self, user_id, role_id):
        roles = self.server.setdefault(user_id, [])
        if role_id not in roles:
            roles.append(role_id)
        self._save()

    def remove_role(self, user_id, role_id):
        if role_id in self.server.get(user_id, []):
            self.server[user_id].remove(role_id)
        self._save()


class NotConfigured:
    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            logger.error("Discord isn't configured (DISCORD_* settings): can't %s", name)
            raise DiscordError("Discord linking isn't available on this site at the moment.")

        return refuse


_fake = FakeDiscord()


def client():
    if configured():
        return DiscordApi()
    if using_fake():
        return fake()
    return NotConfigured()


def fake() -> FakeDiscord:
    global _fake
    if settings.DEBUG and not getattr(settings, "TESTING", False):
        _fake = FakeDiscord(path=str(settings.BASE_DIR / ".fake-discord.json")).load()
    return _fake


def reset_fake() -> FakeDiscord:
    global _fake
    _fake = FakeDiscord()
    return _fake
