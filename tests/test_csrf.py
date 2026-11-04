from __future__ import annotations

import unittest

from app.csrf import CsrfProtector


class CsrfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.csrf = CsrfProtector("a" * 32)

    def test_token_is_bound_to_session(self) -> None:
        first = self.csrf.new_session_id()
        second = self.csrf.new_session_id()
        token = self.csrf.token_for(first)

        self.assertTrue(self.csrf.is_valid(first, token))
        self.assertFalse(self.csrf.is_valid(second, token))
        tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
        self.assertFalse(self.csrf.is_valid(first, tampered))
        self.assertFalse(self.csrf.is_valid("", token))
        self.assertFalse(self.csrf.is_valid(first, ""))

    def test_session_ids_are_random_and_url_safe(self) -> None:
        first = self.csrf.new_session_id()
        second = self.csrf.new_session_id()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 40)
        self.assertRegex(first, r"^[A-Za-z0-9_-]+$")

    def test_secret_is_required(self) -> None:
        with self.assertRaises(ValueError):
            CsrfProtector("")


if __name__ == "__main__":
    unittest.main()
