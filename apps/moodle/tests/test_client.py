import io
import json
import urllib.error
from unittest import mock
from urllib.parse import parse_qs

from django.test import TestCase, override_settings

from apps.moodle import client
from apps.moodle.client import MoodleApi, MoodleError, MoodleUnavailable

from .helpers import member, set_moodle_address


def reply(payload):
    response = mock.MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__.return_value = response
    return response


class MoodleApiTests(TestCase):
    def setUp(self):
        self.api = MoodleApi("https://moodle.example.com/", "made-up-token")
        self.kim = member()

    def test_the_member_is_found_by_their_bbms_id_first(self):
        with mock.patch("urllib.request.urlopen", return_value=reply([{"id": 42}])) as opened:
            self.assertEqual(self.api.find_user_id(self.kim), 42)
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://moodle.example.com/webservice/rest/server.php")
        form = parse_qs(request.data.decode())
        self.assertEqual(form["wsfunction"], ["core_user_get_users_by_field"])
        self.assertEqual((form["field"], form["values[0]"]), (["idnumber"], [str(self.kim.pk)]))
        self.assertEqual(form["wstoken"], ["made-up-token"])
        self.assertEqual(opened.call_count, 1)

    def test_otherwise_by_email_and_otherwise_nobody(self):
        with mock.patch("urllib.request.urlopen", side_effect=[reply([]), reply([{"id": 9}])]) as opened:
            self.assertEqual(self.api.find_user_id(self.kim), 9)
        self.assertEqual(parse_qs(opened.call_args.args[0].data.decode())["field"], ["email"])
        with mock.patch("urllib.request.urlopen", side_effect=[reply([]), reply([])]):
            self.assertIsNone(self.api.find_user_id(self.kim))

    def test_course_status_with_the_time_they_finished(self):
        body = {
            "completionstatus": {
                "completed": True,
                "aggregation": 1,
                "completions": [{"timecompleted": 1_700_000_000}, {"timecompleted": 1_700_000_500}],
            }
        }
        with mock.patch("urllib.request.urlopen", return_value=reply(body)) as opened:
            status = self.api.course_status(42, 7)
        self.assertEqual((status.completed, int(status.completed_at.timestamp())), (True, 1_700_000_500))
        form = parse_qs(opened.call_args.args[0].data.decode())
        self.assertEqual((form["userid"], form["courseid"]), (["42"], ["7"]))

    def test_not_completed(self):
        with mock.patch("urllib.request.urlopen", return_value=reply({"completionstatus": {"completed": False}})):
            status = self.api.course_status(42, 7)
        self.assertEqual((status.completed, status.completed_at), (False, None))

    def test_moodle_refusing_is_an_error_that_doesnt_leak_the_token(self):
        payload = {"exception": "moodle_exception", "errorcode": "invalidtoken", "message": "bad made-up-token"}
        with mock.patch("urllib.request.urlopen", return_value=reply(payload)):
            with self.assertRaises(MoodleError) as caught:
                self.api.course_status(42, 7)
        self.assertIn("invalidtoken", str(caught.exception))
        self.assertNotIn("made-up-token", str(caught.exception))

    def test_an_unreachable_moodle_is_unavailable(self):
        for failure in (urllib.error.URLError("no route"), TimeoutError(), OSError("reset")):
            with mock.patch("urllib.request.urlopen", side_effect=failure):
                with self.assertRaises(MoodleUnavailable):
                    self.api.find_user_id(self.kim)

    def test_a_reply_that_isnt_json_is_unavailable(self):
        response = mock.MagicMock()
        response.read.return_value = io.BytesIO(b"<html>maintenance</html>").read()
        response.__enter__.return_value = response
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(MoodleUnavailable):
                self.api.find_user_id(self.kim)


class WhichClientTests(TestCase):
    @override_settings(MOODLE_TOKEN="")
    def test_without_setup_tests_get_the_fake_and_nothing_is_sent(self):
        self.assertIsInstance(client.client(), client.FakeMoodle)
        self.assertTrue(client.enabled())

    @override_settings(MOODLE_TOKEN="made-up-token")
    def test_with_an_address_and_token_it_is_the_real_one(self):
        set_moodle_address("https://moodle.example.com/")
        self.assertIsInstance(client.client(), client.MoodleApi)
        self.assertEqual(client.course_link(7), "https://moodle.example.com/course/view.php?id=7")

    @override_settings(MOODLE_TOKEN="", DEBUG=False)
    def test_a_live_site_without_setup_cant_check(self):
        with mock.patch.object(client, "using_fake", return_value=False):
            self.assertIsInstance(client.client(), client.NotConfigured)
            self.assertFalse(client.enabled())
            with self.assertRaises(MoodleUnavailable):
                client.client().course_status(1, 1)

    def test_no_address_means_no_course_link(self):
        self.assertEqual(client.course_link(7), "")
