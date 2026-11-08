"""Linking Discord, the welcome message, nicknames and roles (spec 06 §6.14)."""

import io
import json
from io import BytesIO
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.billing import membership
from apps.discord import client, linking
from apps.equipment.tests.helpers import member as make_member
from apps.members.models import Member
from apps.profiles.models import DiscordChannel, RoleInfo
from apps.site_settings.models import SiteSettings

MEMBER, FORMER, WELCOME = client.FakeDiscord.MEMBER_ROLE, client.FakeDiscord.FORMER_ROLE, "channel-welcome"


def set_welcome(text):
    config = SiteSettings.load()
    config.discord_welcome_message = text
    config.save()


class DiscordTestCase(TestCase):
    def setUp(self):
        self.discord = client.reset_fake()

    def joiner(self, **fields):
        return make_member(
            given_name="Kim",
            family_name="Lee",
            status=Member.Status.SETTING_UP,
            monthly_subscription_pence=2500,
            **fields,
        )

    def link(self, who, username="kim_lee", user_id=None):
        """The member links Discord from BBMS, as a browser would."""
        self.client.force_login(who)
        response = self.client.post("/account/discord/connect/")
        state = parse_qs(urlparse(response["Location"]).query)["state"][0]
        code = self.discord.log_in(username, user_id)
        return self.client.get(f"/account/discord/done/?code={code}&state={state}", follow=True)


class LinkingTests(DiscordTestCase):
    def test_a_joiner_links_first(self):
        kim = self.joiner()
        self.client.force_login(kim)
        self.assertContains(self.client.get("/account/"), "Join our Discord server")
        response = self.client.post("/account/payment/direct-debit/", follow=True)
        self.assertContains(response, "link your Discord account")  # payment set-up waits for Discord
        self.assertContains(self.link(kim), "Discord linked as kim_lee. We&#x27;ve added you to our server.")
        kim.refresh_from_db()
        self.assertTrue(kim.discord_on_server)
        self.assertEqual(self.discord.server[kim.discord_user_id], [])  # not a member until they're active
        self.assertEqual(self.discord.nicks[kim.discord_user_id], "Kim Lee")
        self.assertEqual(
            self.discord.messages,
            [
                [
                    WELCOME,
                    f"Welcome to Build Brighton, <@{kim.discord_user_id}>! "
                    "Please read #rules, then pick the topic channels you're interested in "
                    "from Channels & Roles.",
                ]
            ],
        )
        self.assertFalse(linking.blocks_setup(kim))
        self.assertNotContains(self.client.get("/account/payment/"), "link your Discord account")

    def test_already_on_the_server(self):
        kim = make_member(given_name="Kim", family_name="Lee")
        self.discord.server["123"] = []
        self.discord.nicks["123"] = "kimmy"
        self.assertContains(self.link(kim, user_id="123"), "You&#x27;re already on our server.")
        self.assertEqual(self.discord.server["123"], [MEMBER])
        self.assertEqual(self.discord.nicks["123"], "Kim Lee")
        self.assertEqual(self.discord.messages, [])  # only people BBMS adds are welcomed

    def test_one_member_per_discord_account(self):
        make_member(discord_user_id="123", discord_on_server=True)
        kim = self.joiner()
        self.assertContains(self.link(kim, user_id="123"), "linked to another member")
        kim.refresh_from_db()
        self.assertEqual(kim.discord_user_id, "")

    def test_a_bad_state_is_refused(self):
        kim = self.joiner()
        self.client.force_login(kim)
        self.client.post("/account/discord/connect/")
        code = self.discord.log_in("kim")
        response = self.client.get(f"/account/discord/done/?code={code}&state=forged", follow=True)
        self.assertContains(response, "try linking Discord again")
        self.assertFalse(Member.objects.exclude(discord_user_id="").exists())

    def test_said_no_on_discord(self):
        kim = self.joiner()
        self.client.force_login(kim)
        state = parse_qs(urlparse(self.client.post("/account/discord/connect/")["Location"]).query)["state"][0]
        response = self.client.get(f"/account/discord/done/?error=access_denied&state={state}", follow=True)
        self.assertContains(response, "Discord wasn&#x27;t linked")

    def test_discord_down(self):
        kim = self.joiner()
        self.discord.fail_next = "We couldn't reach Discord. Please try again in a few minutes."
        self.assertContains(self.link(kim), "couldn&#x27;t reach Discord")

    def test_exempt(self):
        kim = self.joiner(discord_exempt=True)
        self.assertFalse(linking.blocks_setup(kim))

    def test_unlink(self):
        kim = self.joiner()
        self.link(kim)
        self.client.post("/account/discord/unlink/")
        kim.refresh_from_db()
        self.assertEqual((kim.discord_user_id, kim.discord_on_server), ("", False))
        self.assertTrue(linking.blocks_setup(kim))

    @override_settings(TESTING=False, DEBUG=False)
    def test_not_configured(self):
        self.assertFalse(linking.blocks_setup(self.joiner()))  # a site without Discord doesn't hold anyone up


class RoleTests(DiscordTestCase):
    def linked(self, status=Member.Status.ACTIVE, **fields):
        who = make_member(**{"given_name": "Kim", "family_name": "Lee", "status": status, **fields})
        self.link(who)
        who.refresh_from_db()
        return who

    def test_roles_follow_membership(self):
        kim = self.linked(status=Member.Status.SETTING_UP)
        user = kim.discord_user_id
        with self.captureOnCommitCallbacks(execute=True):
            membership.activate(kim)
        self.assertEqual(self.discord.server[user], [MEMBER])
        kim.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            membership.become_left(kim)
        self.assertEqual(self.discord.server[user], [FORMER])

    def test_nightly_job_puts_things_right(self):
        kim = self.linked()
        user = kim.discord_user_id
        self.discord.server[user] = ["other-role"]
        self.discord.nicks[user] = "Kez"  # their choice, after linking: left alone
        gone = self.linked(given_name="Gone")
        del self.discord.server[gone.discord_user_id]
        Member.objects.filter(pk=gone.pk).update(given_name="Gone")
        out = io.StringIO()
        call_command("sync_discord", stdout=out)
        self.assertEqual(sorted(self.discord.server[user]), ["other-role", MEMBER])  # roles BBMS doesn't manage stay
        self.assertEqual(self.discord.nicks[user], "Kez")
        gone.refresh_from_db()
        self.assertFalse(gone.discord_on_server)
        self.assertIn("not on the server 1", out.getvalue())

    def test_a_welcome_that_failed_is_posted_later(self):
        kim = self.joiner()
        original = self.discord.post
        with mock.patch.object(self.discord, "post", side_effect=client.DiscordError("down")):
            self.link(kim)
        kim.refresh_from_db()
        self.assertTrue(kim.discord_welcome_due)
        self.discord.post = original
        linking.sync_all()
        kim.refresh_from_db()
        self.assertFalse(kim.discord_welcome_due)
        self.assertEqual(len(self.discord.messages), 1)

    def test_welcome_text_from_settings(self):
        set_welcome("Hi {first_name} ({name}) {mention}, see <#42>. @everyone")
        kim = self.joiner()
        self.link(kim)
        kim.refresh_from_db()
        self.assertEqual(
            self.discord.messages[0][1], f"Hi Kim (Kim Lee) <@{kim.discord_user_id}>, see <#42>. @everyone"
        )

    def test_no_welcome_when_its_empty(self):
        set_welcome("")
        self.link(self.joiner())
        self.assertEqual(self.discord.messages, [])

    def test_long_names(self):
        who = Member(given_name="Alexandra", family_name="Featherstonehaugh-Montgomery-Smythe")
        self.assertEqual(linking.nickname_for(who), "Alexandra F.")

    def test_admin_action(self):
        kim = self.linked()
        self.discord.server[kim.discord_user_id] = []
        admin = Member.objects.create_superuser("a@example.com", "a-good-password", given_name="A", family_name="D")
        self.client.force_login(admin)
        self.client.post("/admin/members/member/", {"action": "sync_discord", "_selected_action": [kim.pk]})
        self.assertEqual(self.discord.server[kim.discord_user_id], [MEMBER])


class PageTests(DiscordTestCase):
    @override_settings(DEBUG=True)
    def test_fake_discord_when_developing(self):
        kim = self.joiner()
        self.client.force_login(kim)
        page = self.client.post("/account/discord/connect/", follow=True)
        self.assertContains(page, "Fake Discord")
        state = parse_qs(urlparse(page.redirect_chain[-1][0]).query)["state"][0]
        self.client.post(f"/dev/discord/?state={state}", {"username": "kim_lee"}, follow=True)
        kim.refresh_from_db()
        self.assertEqual(kim.discord_username, "kim_lee")

    def test_fake_discord_is_for_development_only(self):
        self.assertEqual(self.client.get("/dev/discord/").status_code, 404)

    @override_settings(DISCORD_GUILD_ID="777")
    def test_groups_link_to_their_channel(self):
        from django.contrib.auth.models import Group

        laser = Group.objects.create(name="laser")
        RoleInfo.objects.create(group=laser, title="Laser team")
        DiscordChannel.objects.create(group=laser, name="#laser", channel_id="888")
        DiscordChannel.objects.create(group=laser, name="#laser-help")  # no id yet: shown, not linked
        self.client.force_login(make_member())
        page = self.client.get("/groups/")
        self.assertContains(page, 'Discord: <a href="https://discord.com/channels/777/888">#laser</a>, #laser-help')
        self.assertContains(self.client.get("/groups/laser/"), "#laser-help")

    def test_members_see_each_others_discord_name(self):
        kim = make_member(discord_user_id="1", discord_username="kim_lee", discord_on_server=True)
        self.client.force_login(make_member())
        self.assertContains(self.client.get(f"/members/{kim.pk}/"), "kim_lee")


SETTINGS = dict(
    DISCORD_CLIENT_ID="cid",
    DISCORD_CLIENT_SECRET="secret",
    DISCORD_BOT_TOKEN="bot-token",
    DISCORD_GUILD_ID="777",
    DISCORD_MEMBER_ROLE_ID="111",
    DISCORD_FORMER_MEMBER_ROLE_ID="222",
    DISCORD_WELCOME_CHANNEL_ID="333",
)


def reply(status, payload=None):
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.status = status
    response.read.return_value = json.dumps(payload).encode() if payload is not None else b""
    return response


@override_settings(**SETTINGS)
class RealApiTests(TestCase):
    """What BBMS sends to Discord (no network: urlopen is replaced)."""

    def test_link_and_sync(self):
        kim = make_member(given_name="Kim", family_name="Lee", status=Member.Status.SETTING_UP)
        replies = [
            reply(200, {"access_token": "tok"}),
            reply(200, {"id": "555", "username": "kim_lee", "global_name": "Kim"}),
            reply(201, {}),  # added to the server
            reply(200, {"id": "m1"}),  # the welcome message
        ]
        with mock.patch("urllib.request.urlopen", side_effect=replies) as urlopen:
            added = linking.link(kim, "the-code", "https://bbms.example/account/discord/done/")
        self.assertTrue(added)
        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual(requests[0].full_url, "https://discord.com/api/v10/oauth2/token")
        self.assertIn(b"client_secret=secret", requests[0].data)
        self.assertEqual(requests[1].headers["Authorization"], "Bearer tok")
        self.assertEqual(requests[2].get_method(), "PUT")
        self.assertEqual(requests[2].full_url, "https://discord.com/api/v10/guilds/777/members/555")
        self.assertEqual(json.loads(requests[2].data), {"access_token": "tok", "roles": [], "nick": "Kim Lee"})
        self.assertEqual(requests[2].headers["Authorization"], "Bot bot-token")
        self.assertEqual(requests[3].full_url, "https://discord.com/api/v10/channels/333/messages")
        self.assertEqual(json.loads(requests[3].data)["allowed_mentions"], {"parse": [], "users": ["555"]})

        kim.status = Member.Status.ACTIVE
        kim.save()
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=[reply(200, {"roles": ["222"], "nick": "Kim Lee"}), reply(204), reply(204)],
        ) as urlopen:
            self.assertEqual(linking.sync(kim), "+111, -222")
        calls = [(c.args[0].get_method(), c.args[0].full_url.rsplit("/", 1)[-1]) for c in urlopen.call_args_list]
        self.assertEqual(calls, [("GET", "555"), ("PUT", "111"), ("DELETE", "222")])

    def test_left_the_server(self):
        kim = make_member(discord_user_id="555", discord_on_server=True)
        error = HTTPError("u", 404, "Not Found", {}, BytesIO(json.dumps({"code": 10007}).encode()))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertEqual(linking.sync(kim), "not on the server")
        kim.refresh_from_db()
        self.assertFalse(kim.discord_on_server)

    def test_authorize_url(self):
        url = client.client().authorize_url("https://bbms.example/account/discord/done/", "s1")
        params = parse_qs(urlparse(url).query)
        self.assertEqual(
            (params["scope"], params["client_id"], params["state"]), (["identify guilds.join"], ["cid"], ["s1"])
        )
