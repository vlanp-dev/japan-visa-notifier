from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config import ConfigError, load_config
from app.util import mask_email, mask_sms, redact_mapping, redact_text, sanitize_error
from tests.support import TEST_SECRET, config_document, write_config


class ConfigLoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.directory = Path(self._temp.name)

    def test_empty_document_uses_safe_defaults(self) -> None:
        path = self.directory / "empty.json"
        path.write_text("{}", encoding="utf-8")

        config = load_config(path=path, environ={})

        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8000)
        self.assertEqual(config.log_level, "INFO")
        self.assertFalse(config.email_configured)
        self.assertTrue(config.secret_generated)
        self.assertTrue(len(config.secret) >= 32)

    def test_json_and_nested_environment_overrides(self) -> None:
        document = config_document()
        document["email"] = {
            "smtp_host": "smtp.example.org",
            "smtp_port": 25,
            "username": "bot",
            "password": "file-password",
            "sender": "alerts@example.org",
            "recipient": "operator@example.org",
        }
        path = self.directory / "config.json"
        path.write_text(json.dumps(document), encoding="utf-8")

        config = load_config(
            path=path,
            environ={
                "ALERTS__SERVER__PORT": "9100",
                "ALERTS__EMAIL__PASSWORD": "env-password",
                "ALERTS__EMAIL__USE_STARTTLS": "false",
                "ALERTS__LOGGING__LEVEL": "warning",
            },
        )

        self.assertEqual(config.port, 9100)
        self.assertEqual(config.log_level, "WARNING")
        self.assertEqual(config.email.password, "env-password")
        self.assertFalse(config.email.use_starttls)
        self.assertTrue(config.email_configured)

    def test_redacted_configuration_hides_secrets(self) -> None:
        document = config_document()
        document["email"] = {
            "smtp_host": "smtp.example.org",
            "password": "super-secret-password",
            "sender": "alerts@example.org",
            "recipient": "operator@example.org",
        }
        config = write_config(self.directory, document)

        redacted = config.redacted()
        serialized = json.dumps(redacted)

        self.assertNotIn("super-secret-password", serialized)
        self.assertNotIn(TEST_SECRET, serialized)
        self.assertEqual(redacted["email"]["password"], "***")
        self.assertEqual(redacted["server"]["secret"], "***")

    def test_explicit_config_file_must_exist(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(path=self.directory / "missing.json", environ={})

    def test_invalid_json_is_rejected(self) -> None:
        path = self.directory / "config.json"
        path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path=path, environ={})

    def test_partial_email_configuration_is_rejected(self) -> None:
        document = config_document(
            email={"smtp_host": "smtp.example.org", "sender": "alerts@example.org"}
        )
        with self.assertRaises(ConfigError):
            write_config(self.directory, document)

    def test_invalid_values_are_rejected(self) -> None:
        cases = [
            {"server": {"port": 0}},
            {"server": {"port": "not-a-port"}},
            {"server": {"port": 8000.5}},
            {"server": {"secret": "short"}},
            {"logging": {"level": "LOUD"}},
            {"email": {"smtp_host": "smtp.example.org", "sender": "bad", "recipient": "x@example.org"}},
            {
                "email": {
                    "smtp_host": "smtp.example.org",
                    "sender": "alerts@example.org",
                    "recipient": "operator@example.org",
                    "timeout_seconds": float("nan"),
                }
            },
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                document = config_document(**overrides)
                with self.assertRaises(ConfigError):
                    write_config(self.directory, document)

    def test_invalid_environment_override_is_rejected(self) -> None:
        document = config_document()
        path = self.directory / "config.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(
                path=path,
                environ={"ALERTS__SERVER__PORT__NESTED": "1"},
            )

    def test_with_overrides_validates_values(self) -> None:
        document = config_document()
        config = write_config(self.directory, document)
        updated = config.with_overrides(host="0.0.0.0", port=9001)
        self.assertEqual(updated.host, "0.0.0.0")
        self.assertEqual(updated.port, 9001)
        with self.assertRaises(ConfigError):
            config.with_overrides(port=70000)


class RedactionTests(unittest.TestCase):
    def test_redact_mapping_is_recursive_and_preserves_empty_values(self) -> None:
        source = {
            "email": {"password": "hunter2", "username": "bot"},
            "server": {"secret": "", "api_key": "abcd"},
            "items": [{"token": "t"}],
        }
        redacted = redact_mapping(source)
        self.assertEqual(redacted["email"]["password"], "***")
        self.assertEqual(redacted["email"]["username"], "bot")
        self.assertEqual(redacted["server"]["secret"], "")
        self.assertEqual(redacted["server"]["api_key"], "***")
        self.assertEqual(redacted["items"][0]["token"], "***")

    def test_redact_text_masks_common_assignments_and_bearer_tokens(self) -> None:
        text = "login password=hunter2 authorization: Bearer abc.def token=xyz"
        redacted = redact_text(text)
        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("abc.def", redacted)
        self.assertNotIn("xyz", redacted)
        self.assertIn("***", redacted)

    def test_sanitize_error_bounds_length(self) -> None:
        result = sanitize_error("x" * 5000)
        self.assertEqual(len(result), 1000)

    def test_masking(self) -> None:
        self.assertEqual(mask_email("alice@example.org"), "a***@example.org")
        self.assertEqual(mask_email("a@example.org"), "*@example.org")
        self.assertEqual(mask_email("invalid"), "***")
        self.assertEqual(mask_sms("+33 6 12 34 56 78"), "***78")
        self.assertEqual(mask_sms(""), "***")


if __name__ == "__main__":
    unittest.main()
