from django.test import SimpleTestCase

from apps.access import tags

# A made-up fob in the real format (confirmed against a real fob on 2026-11-05; that fob's number isn't kept here).
#   70 0012D687 33: vendor prefix 70, card number 0x0012D687 = 0001234567 in decimal,
#   checksum 70 ^ 00 ^ 12 ^ D6 ^ 87 = 33.
EXAMPLE_TAG = "700012d68733"
EXAMPLE_CARD = 1234567


class TagTests(SimpleTestCase):
    def test_example_fob(self):
        self.assertTrue(tags.is_valid_full_tag(EXAMPLE_TAG))
        self.assertEqual(tags.card_number(EXAMPLE_TAG), EXAMPLE_CARD)
        self.assertEqual(tags.format_card_number(EXAMPLE_CARD), "0001234567")

    def test_building_a_tag_matches_the_example(self):
        self.assertEqual(tags.full_tag("70", EXAMPLE_CARD), EXAMPLE_TAG.upper())

    def test_checksum_must_match(self):
        self.assertFalse(tags.is_valid_full_tag("700012D6873D"))
        self.assertIsNone(tags.card_number("700012D6873D"))

    def test_not_a_full_tag(self):
        for value in ["", "ABCDEF12", "700012D6873300", "ZZ0012D68733", "BB12345678"]:
            self.assertIsNone(tags.card_number(value), value)

    def test_normalise(self):
        self.assertEqual(tags.normalise(" 70 0012 d68733 "), "700012D68733")

    def test_same_card_with_any_prefix_has_same_card_number(self):
        # Today's door firmware guesses prefixes; every guess must give the same card number.
        for prefix in ["55", "BB", "19", "70", "00"]:
            self.assertEqual(tags.card_number(tags.full_tag(prefix, 12345601)), 12345601)
