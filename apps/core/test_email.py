"""Email settings, the deploy check for them, and what happens when the mail server fails (spec 06 §6.6)."""

import json
import os
import smtplib
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import Group
from django.core import mail
from django.core.mail.backends.base import BaseEmailBackend
from django.test import SimpleTestCase, TestCase, override_settings

from apps.billing import emails
from apps.core.checks import email_is_set_up
from apps.members.models import Member

SMTP = "django.core.mail.backends.smtp.EmailBackend"
CONSOLE = "django.core.mail.backends.console.EmailBackend"


def settings_with(**env):
    """The email settings the site would start with, given these environment variables (read in a fresh process)."""
    names = ("EMAIL_BACKEND", "EMAIL_HOST", "EMAIL_PORT", "EMAIL_USE_TLS", "EMAIL_USE_SSL", "EMAIL_TIMEOUT")
    clean = {k: v for k, v in os.environ.items() if not k.startswith("EMAIL_")}
    clean.update({"DEBUG": "True", "EMAIL_HOST": "", **env})
    code = (
        "import json; from django.conf import settings as s; "
        f"print(json.dumps({{n: getattr(s, n) for n in {names!r}}}))"
    )
    run = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(settings.BASE_DIR),
        env={**clean, "DJANGO_SETTINGS_MODULE": "bbms.settings"},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(run.stdout)


class EmailSettingsTests(SimpleTestCase):
    def test_without_a_host_emails_are_only_printed(self):
        found = settings_with()
        self.assertEqual(found["EMAIL_BACKEND"], CONSOLE)

    def test_a_host_turns_on_smtp_with_starttls_on_587(self):
        found = settings_with(EMAIL_HOST="smtp.example.com")
        self.assertEqual(
            (found["EMAIL_BACKEND"], found["EMAIL_PORT"], found["EMAIL_USE_TLS"], found["EMAIL_USE_SSL"]),
            (SMTP, 587, True, False),
        )
        self.assertEqual(found["EMAIL_TIMEOUT"], 10)

    def test_ssl_on_465_switches_starttls_off(self):
        found = settings_with(EMAIL_HOST="smtp.example.com", EMAIL_PORT="465", EMAIL_USE_SSL="True")
        self.assertEqual((found["EMAIL_PORT"], found["EMAIL_USE_TLS"], found["EMAIL_USE_SSL"]), (465, False, True))


class EmailCheckTests(SimpleTestCase):
    def ids(self):
        return [message.id for message in email_is_set_up(None)]

    @override_settings(EMAIL_BACKEND=CONSOLE)
    def test_printing_emails_is_a_warning(self):
        self.assertEqual(self.ids(), ["core.W001"])

    @override_settings(EMAIL_BACKEND=SMTP, EMAIL_HOST="smtp.example.com", EMAIL_USE_TLS=True, EMAIL_USE_SSL=False)
    def test_smtp_with_encryption_is_fine(self):
        self.assertEqual(self.ids(), [])

    @override_settings(EMAIL_BACKEND=SMTP, EMAIL_HOST="")
    def test_smtp_without_a_host_is_a_warning(self):
        self.assertEqual(self.ids(), ["core.W002"])

    @override_settings(EMAIL_BACKEND=SMTP, EMAIL_HOST="smtp.example.com", EMAIL_USE_TLS=False, EMAIL_USE_SSL=False)
    def test_unencrypted_smtp_is_a_warning(self):
        self.assertEqual(self.ids(), ["core.W003"])


class BrokenMailServer(BaseEmailBackend):
    def send_messages(self, email_messages):
        raise smtplib.SMTPRecipientsRefused({"secret.person@example.com": (550, b"no such user")})


@override_settings(EMAIL_BACKEND="apps.core.test_email.BrokenMailServer")
class MailServerFailureTests(TestCase):
    """An email that can't be sent must not undo a payment or stop a nightly job (it was raising before)."""

    def setUp(self):
        self.kim = Member.objects.create_user(
            "secret.person@example.com", "a-good-password", given_name="Kim", family_name="Maker"
        )

    def assert_logged_without_the_address(self, logs):
        text = " ".join(logs.output)
        self.assertIn("Couldn't send an email", text)
        self.assertIn("SMTPRecipientsRefused", text)
        self.assertNotIn("secret.person@example.com", text)

    def test_emailing_a_member_carries_on(self):
        with self.assertLogs("apps.billing.emails", "ERROR") as logs:
            emails.send(self.kim, "members/emails/trusted")
        self.assert_logged_without_the_address(logs)

    def test_emailing_a_non_member_carries_on(self):
        with self.assertLogs("apps.billing.emails", "ERROR") as logs:
            emails.send_to_address("secret.person@example.com", "members/emails/trusted")
        self.assert_logged_without_the_address(logs)

    def test_emailing_a_role_carries_on(self):
        Group.objects.create(name="finance").user_set.add(self.kim)
        with self.assertLogs("apps.billing.emails", "ERROR") as logs:
            emails.tell_role("finance", "members/emails/trusted")
        self.assert_logged_without_the_address(logs)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_a_working_server_still_sends(self):
        emails.send(self.kim, "members/emails/trusted")
        self.assertEqual(len(mail.outbox), 1)
