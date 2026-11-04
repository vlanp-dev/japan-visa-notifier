from __future__ import annotations

import unittest

from app.sms_message import (
    SINGLE_SMS_SEPTETS,
    SmsCompositionError,
    compose_sms,
    gsm7_septets,
    is_gsm7,
    normalize_gsm7,
)


class SmsCompositionTests(unittest.TestCase):
    def test_short_message_includes_acknowledgement_when_it_fits(self) -> None:
        composed = compose_sms(
            "Page modifiée",
            "https://example.org/p/1",
            "https://example.org/a/1",
        )

        self.assertTrue(composed.includes_acknowledgement)
        self.assertLessEqual(composed.septets, SINGLE_SMS_SEPTETS)
        self.assertEqual(composed.segment_count, 1)
        self.assertIn("Page modifiée", composed.text)
        self.assertIn("https://example.org/p/1", composed.text)
        self.assertIn("https://example.org/a/1", composed.text)
        self.assertIn("Acquittement", composed.text)

    def test_acknowledgement_is_dropped_when_message_would_be_too_long(self) -> None:
        page = "https://example.org/p/1"
        acknowledgement = "https://example.org/a/" + "x" * 150

        composed = compose_sms("Page modifiée", page, acknowledgement)

        self.assertFalse(composed.includes_acknowledgement)
        self.assertLessEqual(composed.septets, SINGLE_SMS_SEPTETS)
        self.assertIn("Page modifiée", composed.text)
        self.assertIn(page, composed.text)
        self.assertNotIn(acknowledgement, composed.text)

    def test_required_content_that_cannot_fit_raises_instead_of_being_dropped(self) -> None:
        with self.assertRaises(SmsCompositionError):
            compose_sms("a" * 200, "https://example.org/p/1")

        with self.assertRaises(SmsCompositionError):
            compose_sms(
                "Page modifiée",
                "https://example.org/" + "a" * 200,
            )

    def test_empty_or_invalid_input_is_rejected(self) -> None:
        with self.assertRaises(SmsCompositionError):
            compose_sms("", "https://example.org/p/1")
        with self.assertRaises(SmsCompositionError):
            compose_sms("   ", "https://example.org/p/1")
        with self.assertRaises(SmsCompositionError):
            compose_sms("Page modifiée", "")
        with self.assertRaises(SmsCompositionError):
            compose_sms("Page modifiée", "ftp://example.org/p/1")

    def test_text_is_normalised_to_gsm7(self) -> None:
        composed = compose_sms(
            "Épisode « œuvre » – âge",
            "https://example.org/p/1",
        )

        self.assertTrue(is_gsm7(composed.text))
        self.assertIn("oeuvre", composed.text)
        self.assertIn("age", composed.text)
        self.assertNotIn("œ", composed.text)

    def test_url_is_quoted_to_remain_countable(self) -> None:
        composed = compose_sms(
            "Page modifiée",
            "https://example.org/p/é",
        )

        self.assertTrue(is_gsm7(composed.text))
        self.assertIn("%C3%A9", composed.text)

    def test_gsm_helpers_count_extension_characters_as_two(self) -> None:
        self.assertTrue(is_gsm7("€"))
        self.assertEqual(gsm7_septets("€"), 2)
        self.assertEqual(gsm7_septets("abc"), 3)
        self.assertFalse(is_gsm7("\u00e2"))
        self.assertEqual(normalize_gsm7("\u00e2"), "a")
        self.assertEqual(normalize_gsm7("\u0153"), "oe")
        self.assertEqual(normalize_gsm7("\U0001f600"), "?")


if __name__ == "__main__":
    unittest.main()
