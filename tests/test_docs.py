from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.config import ENV_VARS, Config

ROOT = Path(__file__).resolve().parents[1]


class DocumentationTests(unittest.TestCase):
    def test_readme_documents_endpoints_configuration_and_safety(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")

        for expected in (
            "python3 -m app",
            "config/app.example.json",
            "DETECTION_TARGET_URL",
            "DETECTION_USER_AGENT",
            "GET",
            "/health",
            "/checks/<id>",
            "/rules",
            "/reference/refreeze",
            "closure_block",
            "update_timestamp",
            "new_element",
            "control_failure",
            "network_error",
            "normalis",
            "User-Agent",
            "CSRF",
            "non fiable",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)

    def test_example_configuration_lists_every_supported_key(self) -> None:
        data = json.loads(
            (ROOT / "config" / "app.example.json").read_text(encoding="utf-8")
        )

        self.assertEqual(set(data), set(ENV_VARS))
        config = Config.from_mapping(data)
        self.assertEqual(config.interval_seconds, 900)
        self.assertEqual(config.database_path, "var/detection.sqlite3")


if __name__ == "__main__":
    unittest.main()
