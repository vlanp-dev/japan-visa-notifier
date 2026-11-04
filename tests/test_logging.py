from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from app.config import Config
from app.logging_setup import StructuredLogger
from app.main import create_app
from tests.support import request


class LoggingTests(unittest.TestCase):
    def test_sensitive_content_fields_are_dropped(self) -> None:
        stream = io.StringIO()
        logger = StructuredLogger(stream=stream)

        logger.log(
            "test_event",
            page_content="<html>secret</html>",
            snapshot="{}",
            html="<p>secret</p>",
            excerpt="secret",
            token="secret-token",
            csrf_token="secret-token",
            body="secret-body",
            safe_field="visible",
        )

        line = stream.getvalue().strip()
        record = json.loads(line)
        self.assertEqual(record["event"], "test_event")
        self.assertEqual(record["safe_field"], "visible")
        for forbidden in (
            "page_content",
            "snapshot",
            "html",
            "excerpt",
            "token",
            "csrf_token",
            "body",
        ):
            self.assertNotIn(forbidden, record)
        self.assertNotIn("secret", line.replace("visible", ""))

    def test_log_values_are_bounded(self) -> None:
        stream = io.StringIO()
        logger = StructuredLogger(stream=stream)

        logger.log("test_event", message="x" * 500)

        record = json.loads(stream.getvalue())
        self.assertLessEqual(len(record["message"]), 201)

    def test_request_logs_do_not_contain_page_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stream = io.StringIO()
            logger = StructuredLogger(stream=stream)
            config = Config.from_mapping(
                {
                    "database_path": str(Path(directory) / "detection.sqlite3"),
                    "target_url": "https://exemple.fr/",
                }
            )
            app = create_app(config, logger)

            response = request(
                app,
                "/?page_content=%3Chtml%3ESECRET-PAGE%3C%2Fhtml%3E",
            )
            self.assertEqual(response.status, 200)

            lines = [json.loads(line) for line in stream.getvalue().splitlines()]
            logged = stream.getvalue()

        self.assertNotIn("SECRET-PAGE", logged)
        self.assertTrue(any(line.get("event") == "http_request" for line in lines))
        for line in lines:
            self.assertNotIn("page_content", line)
            self.assertNotIn("snapshot", line)


if __name__ == "__main__":
    unittest.main()
