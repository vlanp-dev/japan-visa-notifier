from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config import DEFAULT_USER_AGENT, Config, ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_defaults_match_foundation_contract(self) -> None:
        config = Config.load(environ={})

        self.assertEqual(config.target_url, "https://example.org/")
        self.assertEqual(config.interval_seconds, 900)
        self.assertEqual(config.request_timeout_seconds, 15.0)
        self.assertEqual(config.max_response_bytes, 2 * 1024 * 1024)
        self.assertEqual(config.user_agent, DEFAULT_USER_AGENT)
        self.assertIn("DetectionCalendrier", config.user_agent)
        self.assertEqual(config.database_path, "var/detection.sqlite3")
        self.assertEqual(config.operator_label, "opérateur")
        self.assertEqual(config.max_request_bytes, 1024 * 1024)
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8080)

    def test_file_overrides_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.json"
            path.write_text(
                json.dumps(
                    {
                        "target_url": "https://exemple.fr/calendrier",
                        "interval_seconds": 1200,
                        "operator_label": "Alice",
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(environ={"DETECTION_CONFIG": str(path)})

        self.assertEqual(config.target_url, "https://exemple.fr/calendrier")
        self.assertEqual(config.interval_seconds, 1200)
        self.assertEqual(config.operator_label, "Alice")
        self.assertEqual(config.request_timeout_seconds, 15.0)

    def test_environment_overrides_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.json"
            path.write_text(
                json.dumps(
                    {
                        "target_url": "https://fichier.example/",
                        "interval_seconds": 1200,
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(
                environ={
                    "DETECTION_CONFIG": str(path),
                    "DETECTION_TARGET_URL": "https://environ.example/page",
                    "DETECTION_INTERVAL_SECONDS": "45",
                    "DETECTION_REQUEST_TIMEOUT_SECONDS": "2.5",
                    "DETECTION_USER_AGENT": "VeilleTest/2.0",
                    "DETECTION_MAX_RESPONSE_BYTES": "4096",
                    "DETECTION_DATABASE_PATH": "/tmp/test.sqlite3",
                    "DETECTION_OPERATOR_LABEL": "Bureau",
                    "DETECTION_MAX_REQUEST_BYTES": "8192",
                    "DETECTION_HOST": "0.0.0.0",
                    "DETECTION_PORT": "9000",
                }
            )

        self.assertEqual(config.target_url, "https://environ.example/page")
        self.assertEqual(config.interval_seconds, 45)
        self.assertEqual(config.request_timeout_seconds, 2.5)
        self.assertEqual(config.user_agent, "VeilleTest/2.0")
        self.assertEqual(config.max_response_bytes, 4096)
        self.assertEqual(config.database_path, "/tmp/test.sqlite3")
        self.assertEqual(config.operator_label, "Bureau")
        self.assertEqual(config.max_request_bytes, 8192)
        self.assertEqual(config.host, "0.0.0.0")
        self.assertEqual(config.port, 9000)

    def test_missing_explicit_configuration_file_is_an_error(self) -> None:
        with self.assertRaises(ConfigError) as raised:
            Config.load(environ={"DETECTION_CONFIG": "/nonexistent/app.json"})
        self.assertIn("not found", str(raised.exception))

    def test_invalid_json_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ConfigError) as raised:
                Config.load(environ={"DETECTION_CONFIG": str(path)})
        self.assertIn("invalid JSON", str(raised.exception))

    def test_invalid_values_are_rejected_together(self) -> None:
        with self.assertRaises(ConfigError) as raised:
            Config.from_mapping(
                {
                    "target_url": "ftp://example.org/",
                    "interval_seconds": 0,
                    "request_timeout_seconds": "abc",
                    "max_response_bytes": -1,
                    "user_agent": "bad\nagent",
                    "database_path": " ",
                    "operator_label": "",
                    "max_request_bytes": 0,
                    "port": 70000,
                }
            )
        message = str(raised.exception)
        for expected in (
            "target_url",
            "interval_seconds",
            "request_timeout_seconds",
            "max_response_bytes",
            "user_agent",
            "database_path",
            "operator_label",
            "max_request_bytes",
            "port",
        ):
            self.assertIn(expected, message)

    def test_unknown_keys_are_rejected(self) -> None:
        with self.assertRaises(ConfigError) as raised:
            Config.from_mapping({"intervale_seconds": 60})
        self.assertIn("unknown configuration key", str(raised.exception))

    def test_url_credentials_are_rejected(self) -> None:
        with self.assertRaises(ConfigError) as raised:
            Config.from_mapping({"target_url": "https://user:secret@example.org/"})
        self.assertIn("credentials", str(raised.exception))

    def test_malformed_urls_are_rejected(self) -> None:
        for url in (
            "https://example.org:notaport/",
            "https://example.org/a b",
            "//example.org/sans-schema",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ConfigError):
                    Config.from_mapping({"target_url": url})


    def test_boolean_is_not_accepted_as_integer(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_mapping({"interval_seconds": True})

    def test_non_finite_timeout_is_rejected(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    Config.from_mapping({"request_timeout_seconds": value})



if __name__ == "__main__":
    unittest.main()
