from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.config import Config
from app.db import connect
from app.main import create_app
from app.security import (
    CSRF_COOKIE,
    SECURITY_HEADERS,
    CsrfMiddleware,
    SecurityHeadersMiddleware,
)
from app.web import Request
from tests.support import request

EXPECTED_SECURITY_HEADERS = {name for name, _ in SECURITY_HEADERS}


def make_config(directory: str, **overrides: Any) -> Config:
    values = {
        "target_url": "https://exemple.fr/calendrier",
        "database_path": str(Path(directory) / "detection.sqlite3"),
        "operator_label": "Bureau des rendez-vous",
    }
    values.update(overrides)
    return Config.from_mapping(values)


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = self._directory.name

    def test_health_returns_json_and_security_headers(self) -> None:
        app = create_app(make_config(self.directory))

        response = request(app, "/health")

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.text), {
            "status": "ok",
            "service": "detection-calendrier",
        })
        self.assertIn("application/json", response.header("Content-Type") or "")
        for name in EXPECTED_SECURITY_HEADERS:
            self.assertIsNotNone(response.header(name), name)
        self.assertEqual(response.header("Cache-Control"), "no-store")
        self.assertIn(CSRF_COOKIE, response.cookies)

    def test_health_reports_degraded_when_schema_is_missing(self) -> None:
        config = make_config(self.directory)
        app = create_app(config)
        connection = connect(config.database_path)
        try:
            connection.execute("DROP TABLE rules")
            connection.commit()
        finally:
            connection.close()

        response = request(app, "/health")

        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.text)["status"], "degraded")

    def test_status_page_is_french_and_escapes_dynamic_values(self) -> None:
        app = create_app(
            make_config(
                self.directory,
                operator_label='<script>alert("x")</script>',
            )
        )

        response = request(app, "/")

        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.header("Content-Type") or "")
        self.assertIn("Veille du calendrier", response.text)
        self.assertIn("https://exemple.fr/calendrier", response.text)
        self.assertNotIn("<script>", response.text)
        self.assertIn("&lt;script&gt;", response.text)
        self.assertIn("900 secondes", response.text)

    def test_not_found_has_security_headers(self) -> None:
        app = create_app(make_config(self.directory))

        response = request(app, "/inconnue")

        self.assertEqual(response.status, 404)
        self.assertIn("Page introuvable", response.text)
        self.assertIsNotNone(response.header("X-Frame-Options"))

    def test_csrf_cookie_is_httponly_and_same_site(self) -> None:
        app = create_app(make_config(self.directory))

        response = request(app, "/")

        set_cookie = response.header("Set-Cookie") or ""
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Lax", set_cookie)
        self.assertIn("Path=/", set_cookie)
        self.assertNotIn("Secure", set_cookie)

    def test_valid_csrf_token_reaches_application(self) -> None:
        app = self._csrf_echo_app()
        first = request(app, "/")
        token = first.cookies[CSRF_COOKIE]

        response = request(
            app,
            "/action",
            method="POST",
            body=f"csrf_token={token}&name=ok".encode(),
            content_type="application/x-www-form-urlencoded",
            cookie=f"{CSRF_COOKIE}={token}",
        )

        self.assertEqual(response.status, 200)
        payload = json.loads(response.text)
        self.assertEqual(payload["form"]["csrf_token"], [token])
        self.assertEqual(payload["form"]["name"], ["ok"])

    def test_missing_csrf_token_is_rejected(self) -> None:
        app = self._csrf_echo_app()
        first = request(app, "/")
        token = first.cookies[CSRF_COOKIE]

        response = request(
            app,
            "/action",
            method="POST",
            body=b"name=ok",
            content_type="application/x-www-form-urlencoded",
            cookie=f"{CSRF_COOKIE}={token}",
        )

        self.assertEqual(response.status, 403)
        self.assertIn("Jeton de sécurité invalide", response.text)
        self.assertIsNotNone(response.header("X-Content-Type-Options"))

    def test_mismatched_csrf_token_is_rejected(self) -> None:
        app = self._csrf_echo_app()
        first = request(app, "/")
        token = first.cookies[CSRF_COOKIE]

        response = request(
            app,
            "/action",
            method="POST",
            body=b"csrf_token=" + b"x" * 43,
            content_type="application/x-www-form-urlencoded",
            cookie=f"{CSRF_COOKIE}={token}",
        )

        self.assertEqual(response.status, 403)

    def test_csrf_rejects_wrong_content_type_and_oversized_body(self) -> None:
        app = self._csrf_echo_app()
        first = request(app, "/")
        token = first.cookies[CSRF_COOKIE]

        wrong_type = request(
            app,
            "/action",
            method="POST",
            body=b"csrf_token=" + token.encode(),
            content_type="application/json",
            cookie=f"{CSRF_COOKIE}={token}",
        )
        oversized = request(
            app,
            "/action",
            method="POST",
            body=b"x" * 2048,
            content_type="application/x-www-form-urlencoded",
            cookie=f"{CSRF_COOKIE}={token}",
        )

        self.assertEqual(wrong_type.status, 415)
        self.assertEqual(oversized.status, 413)

    def test_known_path_with_wrong_method_returns_405(self) -> None:
        app = create_app(make_config(self.directory))
        first = request(app, "/")
        token = first.cookies[CSRF_COOKIE]

        response = request(
            app,
            "/health",
            method="POST",
            body=f"csrf_token={token}".encode(),
            content_type="application/x-www-form-urlencoded",
            cookie=f"{CSRF_COOKIE}={token}",
        )

        self.assertEqual(response.status, 405)
        self.assertEqual(response.header("Allow"), "GET")

    def test_recreating_app_preserves_existing_rows(self) -> None:
        config = make_config(self.directory)
        create_app(config)
        connection = connect(config.database_path)
        try:
            connection.execute(
                """
                INSERT INTO rules
                    (id, name, kind, enabled, parameters, created_at, updated_at)
                VALUES ('r1', 'Règle', 'closure_block', 1, '{}', 'now', 'now')
                """
            )
            connection.commit()
        finally:
            connection.close()

        app = create_app(config)
        response = request(app, "/health")

        self.assertEqual(response.status, 200)
        connection = connect(config.database_path)
        try:
            count = connection.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 1)

    def _csrf_echo_app(self):
        def inner(environ, start_response):
            parsed = Request.from_environ(environ)
            body = json.dumps({"form": parsed.form}).encode("utf-8")
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]

        return SecurityHeadersMiddleware(CsrfMiddleware(inner, 1024))


if __name__ == "__main__":
    unittest.main()
