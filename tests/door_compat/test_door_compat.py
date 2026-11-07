"""
Front-door compatibility tests.

Runs the real BBMSDoorEntry firmware script against a BBMS server and checks that the
door behaves correctly. Any new BBMS server must pass the "ServerContract" tests before
the front door is pointed at it.

    python3 -m unittest -v tests/door_compat/test_door_compat.py

Environment variables:
    BBMS_BACKEND     which server to test (default "legacy-clone"; see backends.py)
    DOOR_FIRMWARE    path to rfid-lock.bash.sh (default ./BBMSDoorEntry/rfid-lock.bash.sh)
    BBMS_CLONE_SSH   ssh command for the legacy clone (see backends.py for the default)
    DOOR_COMPAT_STRICT=1  don't excuse the expected failures on legacy (shows why they fail)

Two kinds of test:
  * ServerContract  - what BBMS must do so the door works safely. Requirements.
  * FirmwareBehaviour - how the current firmware reacts to outages, recorded so we notice
    if it changes. They describe today's behaviour;
    they are not an endorsement of it.
"""

import os
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backends import BACKENDS  # noqa: E402
from door_harness import BbmsProxy, DiscordCapture, DoorProcess, firmware_prefixes, tag_for  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = Path(os.environ.get("DOOR_FIRMWARE", REPO_ROOT / "BBMSDoorEntry" / "rfid-lock.bash.sh"))
PREFIXES = firmware_prefixes(FIRMWARE.read_text())
BACKEND = BACKENDS[os.environ.get("BBMS_BACKEND", "legacy-clone")]()


@dataclass
class Person:
    number: int  # fixture id offset (user id = 9000 + number)
    given_name: str
    family_name: str
    card: str  # what the USB reader types (10 decimal digits)
    prefix_index: int | None  # which firmware prefix matches their fob (None = no fob in BBMS)
    status: str = "active"
    active: bool = True
    trusted: bool = True
    key_holder: bool = True
    photo: bool = True
    photo_on_wall: bool = False
    has_profile: bool = True
    fob_active: bool = True

    @property
    def tag_id(self) -> str | None:
        if self.prefix_index is None:
            return None
        return tag_for(int(self.card), PREFIXES[self.prefix_index])

    @property
    def name(self) -> str:
        return f"{self.given_name} {self.family_name}"


KEYHOLDER = Person(1, "Kim", "Keyholder", "0012345601", prefix_index=0)
LATE_PREFIX = Person(2, "Lee", "Laterprefix", "0012345602", prefix_index=9, photo=False, photo_on_wall=True)
NOT_KEYHOLDER = Person(3, "Nat", "Notkeyholder", "0012345603", prefix_index=0, trusted=False, key_holder=False)
LEFT = Person(4, "Lou", "Left", "0012345604", prefix_index=0, status="left", active=False)
NO_PHOTO = Person(5, "Nia", "Nophoto", "0012345605", prefix_index=0, photo=False)
LOST_FOB = Person(6, "Les", "Lostfob", "0012345606", prefix_index=0, fob_active=False)
NO_PROFILE = Person(7, "Pat", "Noprofile", "0012345607", prefix_index=0, has_profile=False)
REVOKE_FOB = Person(8, "Rae", "Revokedfob", "0012345608", prefix_index=0)
REVOKE_KEY = Person(9, "Ray", "Revokedkey", "0012345609", prefix_index=0)
UNKNOWN_CARD = "0012345699"

PEOPLE = [KEYHOLDER, LATE_PREFIX, NOT_KEYHOLDER, LEFT, NO_PHOTO, LOST_FOB, NO_PROFILE, REVOKE_FOB, REVOKE_KEY]


def differs_on_legacy(reason):
    """Expected to fail on the legacy server (documents a difference); must pass on the new one."""

    def decorate(test):
        if BACKEND.name == "legacy-clone" and os.environ.get("DOOR_COMPAT_STRICT") != "1":
            test = unittest.expectedFailure(test)
        test.__doc__ = (test.__doc__ or "") + f"  [differs on legacy: {reason}]"
        return test

    return decorate


def setUpModule():
    BACKEND.start()
    BACKEND.install_fixtures(PEOPLE)


def tearDownModule():
    try:
        BACKEND.remove_fixtures()
    finally:
        BACKEND.stop()


class DoorTestCase(unittest.TestCase):
    heartbeat_seconds = 900
    preload_cache = None

    def setUp(self):
        self.proxy = BbmsProxy(
            BACKEND.https_host,
            BACKEND.https_port,
            BACKEND.host_header,
            BACKEND.verify_tls,
            use_tls=getattr(BACKEND, "use_tls", True),
        )
        self.discord = DiscordCapture()
        self.door = DoorProcess(
            FIRMWARE,
            self.proxy,
            self.discord,
            BACKEND.api_key,
            BACKEND.device_name,
            heartbeat_seconds=self.heartbeat_seconds,
            preload_cache=self.preload_cache,
        )

    def tearDown(self):
        self.door.stop()
        self.proxy.close()
        self.discord.close()

    def assertRefusedOnline(self, result, attempts):
        self.assertFalse(result.door_opened, "door must stay shut")
        self.assertEqual(len(result.statuses), attempts, f"expected {attempts} lookups, got {result.statuses}")
        for status in result.statuses:
            self.assertTrue(
                status is not None and 400 <= status < 500,
                f"refusals must be 4xx (5xx makes the door go offline): {result.statuses}",
            )
        self.assertNotIn(result.card, self.door.cached())


class ServerContract(DoorTestCase):
    """Requirements on BBMS so the current front door works safely."""

    def test_boot_is_accepted_and_recorded(self):
        boots = [e for e in self.proxy.exchanges if e.path == "/acs/node/boot"]
        self.assertEqual(len(boots), 1)
        self.assertTrue(200 <= boots[0].status < 300, boots[0])
        self.assertIsNotNone(BACKEND.last_boot())

    def test_keyholder_is_let_in_cached_and_announced(self):
        before = BACKEND.entries_recorded(KEYHOLDER)
        result = self.door.tap(KEYHOLDER.card)
        self.assertTrue(result.door_opened, result.log)
        self.assertEqual(len(result.statuses), 1, "first prefix matches, so one lookup")
        self.assertTrue(200 <= result.statuses[0] < 300)
        self.assertEqual(self.door.cached().get(KEYHOLDER.card), KEYHOLDER.tag_id)
        self.assertEqual(result.discord, [f"{KEYHOLDER.name} is in the space!"])
        self.assertEqual(BACKEND.entries_recorded(KEYHOLDER), before + 1)

    def test_request_shape_matches_firmware(self):
        result = self.door.tap(KEYHOLDER.card)
        body = result.exchanges[-1].body
        self.assertEqual(set(body), {"tagId", "device", "occurredAt"})
        self.assertEqual(body["device"], BACKEND.device_name)
        self.assertEqual(body["tagId"], KEYHOLDER.tag_id)

    def test_vendor_prefix_guessing_reaches_later_prefixes(self):
        """A fob whose vendor prefix is late in the firmware's list still gets in.

        Legacy matches the exact tag, so wrong-prefix guesses must be clean 4xx refusals and the door keeps
        trying. The new system also matches on the card number (05 §5.1), so the first guess already works.
        """
        result = self.door.tap(LATE_PREFIX.card)
        self.assertTrue(result.door_opened, result.log)
        if getattr(BACKEND, "matches_card_number", False):
            self.assertEqual(len(result.statuses), 1, "card-number matching: the first guess opens the door")
            self.assertIn(LATE_PREFIX.card, self.door.cached())
        else:
            attempts = LATE_PREFIX.prefix_index + 1
            self.assertEqual(len(result.statuses), attempts)
            self.assertTrue(all(400 <= s < 500 for s in result.statuses[:-1]), result.statuses)
            self.assertEqual(self.door.cached().get(LATE_PREFIX.card), LATE_PREFIX.tag_id)

    def test_photo_on_wall_counts_instead_of_online_photo(self):
        self.assertTrue(self.door.tap(LATE_PREFIX.card).door_opened)

    def test_cached_fob_is_still_checked_with_server_when_online(self):
        self.door.tap(KEYHOLDER.card)
        again = self.door.tap(KEYHOLDER.card)
        self.assertTrue(again.door_opened)
        self.assertEqual(len(again.statuses), 1)
        self.assertEqual(again.exchanges[-1].tag_id, KEYHOLDER.tag_id)

    def test_member_who_is_not_a_keyholder_is_refused(self):
        self.assertRefusedOnline(self.door.tap(NOT_KEYHOLDER.card), attempts=len(PREFIXES))

    def test_member_who_has_left_is_refused(self):
        self.assertRefusedOnline(self.door.tap(LEFT.card), attempts=len(PREFIXES))

    def test_keyholder_without_any_photo_is_refused(self):
        self.assertRefusedOnline(self.door.tap(NO_PHOTO.card), attempts=len(PREFIXES))

    def test_lost_fob_is_refused(self):
        self.assertRefusedOnline(self.door.tap(LOST_FOB.card), attempts=len(PREFIXES))

    def test_unknown_fob_is_refused_after_trying_every_prefix(self):
        self.assertRefusedOnline(self.door.tap(UNKNOWN_CARD), attempts=len(PREFIXES))

    @differs_on_legacy("member with no profile row causes HTTP 500")
    def test_server_errors_never_used_for_refusals(self):
        """Every refusal must be 4xx: a 5xx makes the door treat BBMS as offline."""
        self.assertRefusedOnline(self.door.tap(NO_PROFILE.card), attempts=len(PREFIXES))

    def test_lost_fob_is_removed_from_door_cache(self):
        self.assertTrue(self.door.tap(REVOKE_FOB.card).door_opened)
        self.assertIn(REVOKE_FOB.card, self.door.cached())
        BACKEND.set_fob_active(REVOKE_FOB, False)
        try:
            result = self.door.tap(REVOKE_FOB.card)
            self.assertFalse(result.door_opened)
            self.assertNotIn(REVOKE_FOB.card, self.door.cached(), "revoked fob must be uncached")
        finally:
            BACKEND.set_fob_active(REVOKE_FOB, True)

    def test_removed_keyholder_status_is_removed_from_door_cache(self):
        self.assertTrue(self.door.tap(REVOKE_KEY.card).door_opened)
        BACKEND.set_keyholder(REVOKE_KEY, False)
        try:
            result = self.door.tap(REVOKE_KEY.card)
            self.assertFalse(result.door_opened)
            self.assertNotIn(REVOKE_KEY.card, self.door.cached())
        finally:
            BACKEND.set_keyholder(REVOKE_KEY, True)


class Heartbeat(DoorTestCase):
    heartbeat_seconds = 3

    def test_idle_door_sends_heartbeat_and_server_accepts_it(self):
        self.door.wait_until_ready(timeout=30)  # one idle period elapses
        beats = [e for e in self.proxy.exchanges if e.path == "/acs/node/heartbeat"]
        self.assertGreaterEqual(len(beats), 1)
        self.assertTrue(all(200 <= e.status < 300 for e in beats), beats)


class FirmwareBehaviour(DoorTestCase):
    """How today's firmware behaves in outages (characterisation; see spec 05)."""

    preload_cache = {KEYHOLDER.card: KEYHOLDER.tag_id}

    def test_unreachable_server_cached_fob_opens_only_after_a_failed_request(self):
        self.proxy.set_mode("down")
        first = self.door.tap(KEYHOLDER.card)  # previous request succeeded, so it asks BBMS first
        self.assertFalse(first.door_opened, "first tap after outage starts is refused")
        second = self.door.tap(KEYHOLDER.card)  # now 'offline': cached fob opens
        self.assertTrue(second.door_opened)
        self.assertIn(KEYHOLDER.card, self.door.cached(), "an outage must not empty the cache")

    def test_unreachable_server_unknown_fob_gives_up_after_one_attempt(self):
        self.proxy.set_mode("down")
        result = self.door.tap(NOT_KEYHOLDER.card)
        self.assertFalse(result.door_opened)
        self.assertTrue(any("Server is down" in line for line in result.log), result.log)

    def test_server_error_5xx_is_treated_as_offline(self):
        self.proxy.set_mode("error")
        self.door.tap(KEYHOLDER.card)
        result = self.door.tap(KEYHOLDER.card)
        self.assertTrue(result.door_opened, "5xx => offline => cached fob opens")
        self.assertIn(KEYHOLDER.card, self.door.cached())

    def test_after_outage_cached_fob_is_let_in_before_server_is_asked(self):
        """After an outage, a cached fob is let in before BBMS is asked."""
        self.proxy.set_mode("down")
        self.door.tap(KEYHOLDER.card)
        self.door.tap(KEYHOLDER.card)
        self.proxy.set_mode("online")
        result = self.door.tap(KEYHOLDER.card)
        self.assertTrue(result.door_opened)
        self.assertEqual(len(result.statuses), 1, "it still reports the entry to BBMS afterwards")
        opened_at = next(i for i, line in enumerate(result.log) if line.startswith("opening door to"))
        asked_at = next(i for i, line in enumerate(result.log) if ": activity <<" in line)
        self.assertLess(opened_at, asked_at, "door opens before BBMS is asked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
