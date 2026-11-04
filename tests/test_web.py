from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.http import Response
from app.repositories import EpisodeRepository
from app.web import create_app
from tests.support import WSGIClient, null_logger, temporary_config


class WebApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._holder, self.config = temporary_config()
        self.addCleanup(self._holder.cleanup)
        self.app = create_app(self.config, logger=null_logger())
        self.client = WSGIClient(self.app)

    def _token(self) -> str:
        self.client.get("/")
        return self.app.csrf.token_for(self.client.cookies["alerts_session"])

    def test_health_json(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status, 200)
        self.assertIn("application/json", response.header("Content-Type"))
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["database"]["status"], "ok")
        self.assertEqual(payload["email"]["status"], "unconfigured")
        self.assertIn("version", payload)

    def test_healthz_alias(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_email_configuration_is_reported_without_secrets(self) -> None:
        holder, config = temporary_config(
            email={
                "smtp_host": "smtp.example.org",
                "username": "bot",
                "password": "super-secret-password",
                "sender": "alerts@example.org",
                "recipient": "operator@example.org",
            }
        )
        self.addCleanup(holder.cleanup)
        app = create_app(config, logger=null_logger())
        client = WSGIClient(app)

        payload = client.get("/health").json()
        page = client.get("/").text

        self.assertEqual(payload["email"]["status"], "configured")
        self.assertEqual(payload["email"]["provider"], "smtp")
        self.assertIn("Configuré", page)
        self.assertNotIn("super-secret-password", page)

    def test_health_page_is_html_and_sets_security_headers(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.header("Content-Type"))
        self.assertIn("Service opérationnel", response.text)
        self.assertIn("Non configuré", response.text)
        self.assertIn("Alertes de test", response.text)

        self.assertEqual(response.header("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.header("X-Frame-Options"), "DENY")
        self.assertEqual(response.header("Referrer-Policy"), "no-referrer")
        self.assertIn("default-src 'none'", response.header("Content-Security-Policy"))
        self.assertEqual(response.header("Cache-Control"), "no-store")

        cookie = response.header("Set-Cookie")
        self.assertIn("alerts_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("Secure", cookie)

    def test_dynamic_values_are_escaped_in_html(self) -> None:
        holder, config = temporary_config(
            service={"name": '<script>alert("service")</script>', "environment": "test"}
        )
        self.addCleanup(holder.cleanup)
        app = create_app(config, logger=null_logger())
        episodes = EpisodeRepository(app.database)
        episodes.create(
            change_nature="<img src=x onerror=alert(1)>",
            page_url="https://example.org/page?q=<script>",
        )
        client = WSGIClient(app)

        text = client.get("/").text

        self.assertNotIn("<script>alert", text)
        self.assertNotIn("<img src=x", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", text)
        self.assertIn("&lt;script&gt;", text)

    def test_delivery_attempts_are_visible_and_masked(self) -> None:
        attempt = self.app.deliveries.record(
            channel="email",
            kind="alert",
            status="failed",
            episode_id=None,
            provider="smtp",
            recipient="alice@example.org",
            error="smtp password=hunter2 refused",
        )

        text = self.client.get("/").text

        self.assertIn("a***@example.org", text)
        self.assertIn("failed", text)
        self.assertNotIn("alice@example.org", text)
        self.assertNotIn("hunter2", text)
        self.assertIn("password=***", text)
        self.assertEqual(attempt.recipient_masked, "a***@example.org")

    def test_not_found(self) -> None:
        response = self.client.get("/does-not-exist")
        self.assertEqual(response.status, 404)
        self.assertIn("Ressource introuvable", response.text)

    def test_method_not_allowed_has_allow_header(self) -> None:
        token = self._token()
        response = self.client.request(
            "PUT", "/health", headers={"X-CSRF-Token": token}
        )
        self.assertEqual(response.status, 405)
        self.assertIn("GET", response.header("Allow"))
        self.assertIn("HEAD", response.header("Allow"))

    def test_head_returns_headers_without_body(self) -> None:
        response = self.client.head("/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"")
        self.assertGreater(int(response.header("Content-Length")), 0)
        self.assertIn("application/json", response.header("Content-Type"))

    def test_health_degraded_when_database_check_fails(self) -> None:
        with mock.patch.object(
            self.app.database, "check", side_effect=RuntimeError("boom")
        ):
            response = self.client.get("/health")

        self.assertEqual(response.status, 503)
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(response.json()["database"]["status"], "error")

    def test_health_page_degraded_when_database_check_fails(self) -> None:
        with mock.patch.object(
            self.app.database, "check", side_effect=RuntimeError("boom")
        ):
            response = self.client.get("/")

        self.assertEqual(response.status, 200)
        self.assertIn("Service dégradé", response.text)
        self.assertIn("Indisponible", response.text)


class CsrfHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._holder, self.config = temporary_config()
        self.addCleanup(self._holder.cleanup)

        def action(request):
            return Response.text("action ok")

        self.app = create_app(
            self.config,
            logger=null_logger(),
            extra_routes=[("POST", "/action", action)],
        )
        self.client = WSGIClient(self.app)

    def test_unsafe_request_without_token_is_rejected(self) -> None:
        response = self.client.post_form("/action")
        self.assertEqual(response.status, 403)
        self.assertIn("CSRF", response.text)

    def test_unsafe_request_with_valid_header_token_succeeds(self) -> None:
        self.client.get("/")
        session_id = self.client.cookies["alerts_session"]
        token = self.app.csrf.token_for(session_id)

        response = self.client.request(
            "POST", "/action", headers={"X-CSRF-Token": token}
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.text, "action ok")

    def test_unsafe_request_with_valid_form_token_succeeds(self) -> None:
        self.client.get("/")
        session_id = self.client.cookies["alerts_session"]
        token = self.app.csrf.token_for(session_id)

        response = self.client.post_form("/action", {"csrf_token": token})

        self.assertEqual(response.status, 200)

    def test_token_for_another_session_is_rejected(self) -> None:
        self.client.get("/")
        session_id = self.client.cookies["alerts_session"]
        token = self.app.csrf.token_for(session_id)

        response = self.client.request(
            "POST",
            "/action",
            headers={"X-CSRF-Token": token},
            cookies={"alerts_session": "x" * 43},
        )

        self.assertEqual(response.status, 403)

    def test_oversized_body_is_rejected(self) -> None:
        self.client.get("/")
        session_id = self.client.cookies["alerts_session"]
        token = self.app.csrf.token_for(session_id)
        body = b"a" * (70 * 1024)

        response = self.client.request(
            "POST",
            "/action",
            body=body,
            headers={"X-CSRF-Token": token},
            content_type="application/x-www-form-urlencoded",
        )

        self.assertEqual(response.status, 413)

    def test_unsupported_content_type_is_rejected(self) -> None:
        self.client.get("/")
        session_id = self.client.cookies["alerts_session"]
        token = self.app.csrf.token_for(session_id)

        response = self.client.request(
            "POST",
            "/action",
            body=b"{}",
            headers={"X-CSRF-Token": token},
            content_type="application/json",
        )

        self.assertEqual(response.status, 415)

    def test_path_parameters_are_available_to_handlers(self) -> None:
        def handler(request):
            return Response.text(request.path_params["episode_id"])

        self.app.add_route("GET", "/episodes/{episode_id}", handler)
        response = self.client.get("/episodes/abc123")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.text, "abc123")

    def test_production_cookies_are_secure_and_hsts_is_set(self) -> None:
        holder, config = temporary_config(
            service={"name": "Prod", "environment": "production"}
        )
        self.addCleanup(holder.cleanup)
        app = create_app(config, logger=null_logger())
        response = WSGIClient(app).get("/")

        self.assertIn("Secure", response.header("Set-Cookie"))
        self.assertIn("max-age=31536000", response.header("Strict-Transport-Security"))


class EmailChannelWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.outbox = Path(self._temp.name) / "outbox"
        self._holder, self.config = temporary_config(
            service={
                "name": "Alertes de test",
                "environment": "test",
                "base_url": "https://alerts.example.org",
            },
            email={
                "provider": "outbox",
                "outbox_path": str(self.outbox),
                "sender": "alerts@example.org",
                "recipient": "operator@example.org",
            },
        )
        self.addCleanup(self._holder.cleanup)
        self.app = create_app(self.config, logger=null_logger())
        self.client = WSGIClient(self.app)

    def _token(self) -> str:
        self.client.get("/")
        return self.app.csrf.token_for(self.client.cookies["alerts_session"])

    def _create_episode(self, **overrides) -> str:
        form = {
            "csrf_token": self._token(),
            "change_nature": "Ouverture de la pêche",
            "page_url": "https://example.org/reglement",
            "summary": "Le règlement a changé.",
        }
        form.update(overrides)
        response = self.client.post_form("/episodes", form)
        self.assertEqual(response.status, 303)
        return response.header("Location")

    def test_new_episode_form_is_available(self) -> None:
        response = self.client.get("/episodes/new")

        self.assertEqual(response.status, 200)
        self.assertIn("Créer un épisode", response.text)
        self.assertIn('name="csrf_token"', response.text)

    def test_create_episode_sends_email_and_redirects_to_detail(self) -> None:
        location = self._create_episode()

        self.assertTrue(location.startswith("/episodes/"))
        self.assertIn("notice=created", location)
        self.assertEqual(len(list(self.outbox.glob("*.eml"))), 1)

        detail = self.client.get(location.split("?", 1)[0])
        self.assertEqual(detail.status, 200)
        self.assertIn("Ouverture de la pêche", detail.text)
        self.assertIn("https://example.org/reglement", detail.text)
        self.assertIn("sent", detail.text)
        self.assertIn("Acquitter l'épisode", detail.text)

    def test_delivery_log_lists_email_attempts(self) -> None:
        self._create_episode()

        response = self.client.get("/deliveries")

        self.assertEqual(response.status, 200)
        self.assertIn("email", response.text)
        self.assertIn("o***@example.org", response.text)
        self.assertNotIn("operator@example.org", response.text)

    def test_acknowledgement_updates_episode(self) -> None:
        location = self._create_episode()
        episode_id = location.split("?", 1)[0].rsplit("/", 1)[-1]

        response = self.client.post_form(
            f"/episodes/{episode_id}/acknowledge",
            {"csrf_token": self._token()},
        )

        self.assertEqual(response.status, 303)
        detail = self.client.get(f"/episodes/{episode_id}")
        self.assertIn("Acquitté le", detail.text)
        self.assertIsNotNone(self.app.episodes.get(episode_id).acknowledged_at)

    def test_create_episode_rejects_invalid_input(self) -> None:
        response = self.client.post_form(
            "/episodes",
            {
                "csrf_token": self._token(),
                "change_nature": "",
                "page_url": "javascript:alert(1)",
            },
        )

        self.assertEqual(response.status, 400)
        self.assertIn("Créer un épisode", response.text)

    def test_create_episode_requires_csrf_token(self) -> None:
        response = self.client.post_form(
            "/episodes",
            {
                "change_nature": "Nature",
                "page_url": "https://example.org/page",
            },
        )

        self.assertEqual(response.status, 403)

    def test_unknown_episode_returns_404(self) -> None:
        response = self.client.get("/episodes/unknown")
        self.assertEqual(response.status, 404)

    def test_email_failure_is_visible_and_does_not_block_ack_or_resend(self) -> None:
        class FailingProvider:
            name = "fake"

            def __init__(self) -> None:
                self.calls = 0

            def send(self, message) -> None:
                self.calls += 1
                raise RuntimeError("fournisseur email indisponible")

        provider = FailingProvider()
        app = create_app(
            self.config, email_provider=provider, logger=null_logger()
        )
        client = WSGIClient(app)
        client.get("/")
        token = app.csrf.token_for(client.cookies["alerts_session"])

        created = client.post_form(
            "/episodes",
            {
                "csrf_token": token,
                "change_nature": "Nature",
                "page_url": "https://example.org/page",
            },
        )
        self.assertEqual(created.status, 303)
        location = created.header("Location")
        episode_id = location.split("?", 1)[0].rsplit("/", 1)[-1]

        detail = client.get(location.split("?", 1)[0])
        self.assertIn("failed", detail.text)
        self.assertIn("fournisseur email indisponible", detail.text)

        acknowledged = client.post_form(
            f"/episodes/{episode_id}/acknowledge",
            {"csrf_token": app.csrf.token_for(client.cookies["alerts_session"])},
        )
        self.assertEqual(acknowledged.status, 303)

        resent = client.post_form(
            f"/episodes/{episode_id}/resend",
            {"csrf_token": app.csrf.token_for(client.cookies["alerts_session"])},
        )
        self.assertEqual(resent.status, 303)
        self.assertIn("notice=resent_failed", resent.header("Location"))
        self.assertEqual(provider.calls, 2)
        attempts = app.deliveries.list_for_episode(episode_id)
        self.assertEqual([attempt.kind for attempt in attempts], ["alert", "resend"])
        self.assertTrue(all(attempt.status == "failed" for attempt in attempts))


class ErrorHandlingTests(unittest.TestCase):
    def test_unhandled_exception_returns_generic_500(self) -> None:
        holder, config = temporary_config()
        self.addCleanup(holder.cleanup)

        def broken(request):
            raise RuntimeError("secret=should-not-leak")

        app = create_app(
            config,
            logger=null_logger(),
            extra_routes=[("GET", "/broken", broken)],
        )
        response = WSGIClient(app).get("/broken")

        self.assertEqual(response.status, 500)
        self.assertIn("erreur interne", response.text.lower())
        self.assertNotIn("should-not-leak", response.text)


if __name__ == "__main__":
    unittest.main()
