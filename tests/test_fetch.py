from __future__ import annotations

import io
import time
import unittest
from pathlib import Path

from app.config import DEFAULT_USER_AGENT, Config
from app.detection.fetch import (
    MAX_REDIRECTS,
    FailureCategory,
    Fetcher,
    fetch_page,
)
from app.logging_setup import StructuredLogger
from tests.fetch_support import (
    LoopbackServer,
    closed_port_url,
    respond_after,
    respond_drip_body,
    respond_drip_headers,
    respond_html,
    respond_raw,
    respond_redirect,
)

FIXTURES = Path(__file__).with_name("fixtures")
CLOSED_PAGE = (FIXTURES / "closed_page.html").read_bytes()


def make_fetcher(
    url: str,
    *,
    timeout: float = 2.0,
    max_response_bytes: int = 2 * 1024 * 1024,
    max_redirects: int = MAX_REDIRECTS,
    logger: StructuredLogger | None = None,
) -> Fetcher:
    config = Config.from_mapping(
        {
            "target_url": url,
            "request_timeout_seconds": timeout,
            "max_response_bytes": max_response_bytes,
        }
    )
    return Fetcher(
        config,
        logger=logger,
        max_redirects=max_redirects,
    )


class FetchSuccessTests(unittest.TestCase):
    def test_successful_fetch_returns_snapshot_and_metadata(self) -> None:
        with LoopbackServer() as server:
            server.route(
                "/",
                respond_html(
                    CLOSED_PAGE,
                    headers={
                        "ETag": '"closed-2026"',
                        "Last-Modified": "Wed, 04 Nov 2026 09:30:00 GMT",
                    },
                ),
            )
            result = make_fetcher(server.url("/")).fetch()

        self.assertTrue(result.ok)
        self.assertIsNone(result.failure)
        self.assertIsNotNone(result.snapshot)
        assert result.snapshot is not None
        self.assertIn("Complet 2026", result.snapshot.text)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.etag, '"closed-2026"')
        self.assertEqual(result.content_type, "text/html")
        self.assertEqual(
            result.last_modified, "Wed, 04 Nov 2026 09:30:00 GMT"
        )
        self.assertEqual(result.final_url, server.url("/"))
        self.assertEqual(result.redirects, 0)
        self.assertEqual(len(server.requests), 1)

    def test_one_get_with_explicit_user_agent_and_no_credentials(self) -> None:
        with LoopbackServer() as server:
            server.route("/", respond_html(CLOSED_PAGE))
            result = make_fetcher(server.url("/")).fetch()

        self.assertTrue(result.ok)
        self.assertEqual(len(server.requests), 1)
        recorded = server.requests[0]
        self.assertEqual(recorded.method, "GET")
        self.assertEqual(
            recorded.headers.get("user-agent"), DEFAULT_USER_AGENT
        )
        self.assertNotIn("cookie", recorded.headers)
        self.assertNotIn("authorization", recorded.headers)
        self.assertIn("text/html", recorded.headers.get("accept", ""))

    def test_xhtml_content_type_is_accepted(self) -> None:
        body = (
            b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
            b"<body><p>Complet 2026</p></body></html>"
        )
        with LoopbackServer() as server:
            server.route(
                "/",
                respond_html(
                    body,
                    content_type="application/xhtml+xml; charset=utf-8",
                ),
            )
            result = make_fetcher(server.url("/")).fetch()

        self.assertTrue(result.ok)
        assert result.snapshot is not None
        self.assertIn("Complet 2026", result.snapshot.text)

    def test_declared_charset_is_honoured(self) -> None:
        body = (
            "<html><body><p>Dernière mise à jour : 4 novembre 2026</p>"
            "</body></html>"
        ).encode("iso-8859-1")
        with LoopbackServer() as server:
            server.route(
                "/",
                respond_html(
                    body,
                    content_type="text/html; charset=iso-8859-1",
                ),
            )
            result = make_fetcher(server.url("/")).fetch()

        self.assertTrue(result.ok)
        assert result.snapshot is not None
        self.assertIn("Dernière mise à jour", result.snapshot.text)

    def test_fetch_page_convenience_wrapper(self) -> None:
        with LoopbackServer() as server:
            server.route("/", respond_html(b"<html><body>ok</body></html>"))
            config = Config.from_mapping(
                {"target_url": server.url("/"), "request_timeout_seconds": 2}
            )
            result = fetch_page(config)

        self.assertTrue(result.ok)


class RedirectTests(unittest.TestCase):
    def test_five_redirects_are_followed(self) -> None:
        with LoopbackServer() as server:
            for index in range(5):
                server.route(
                    f"/r{index}",
                    respond_redirect(f"/r{index + 1}"),
                )
            server.route(
                "/r5", respond_html(b"<html><body>arrivee</body></html>")
            )
            result = make_fetcher(server.url("/r0")).fetch()

        self.assertTrue(result.ok)
        self.assertEqual(result.redirects, 5)
        self.assertEqual(len(server.requests), 6)
        self.assertEqual(result.final_url, server.url("/r5"))

    def test_sixth_redirect_is_refused(self) -> None:
        with LoopbackServer() as server:
            for index in range(6):
                server.route(
                    f"/r{index}",
                    respond_redirect(f"/r{index + 1}"),
                )
            server.route(
                "/r6", respond_html(b"<html><body>trop loin</body></html>")
            )
            result = make_fetcher(server.url("/r0")).fetch()

        self.assertFalse(result.ok)
        assert result.failure is not None
        self.assertEqual(result.failure.category, FailureCategory.HTTP_STATUS)
        self.assertEqual(result.redirects, 5)
        # The refused redirect is the sixth response; its target is not fetched.
        self.assertEqual(len(server.requests), 6)
        self.assertEqual(server.requests[-1].path, "/r5")

    def test_redirect_loop_hits_the_cap_without_retrying(self) -> None:
        with LoopbackServer() as server:
            server.route("/loop", respond_redirect("/loop"))
            result = make_fetcher(server.url("/loop")).fetch()

        self.assertFalse(result.ok)
        assert result.failure is not None
        self.assertEqual(result.failure.category, FailureCategory.HTTP_STATUS)
        self.assertEqual(len(server.requests), 6)

    def test_redirect_to_non_http_scheme_is_refused(self) -> None:
        with LoopbackServer() as server:
            server.route("/", respond_redirect("file:///etc/passwd"))
            result = make_fetcher(server.url("/")).fetch()

        self.assertFalse(result.ok)
        assert result.failure is not None
        self.assertEqual(
            result.failure.category, FailureCategory.NETWORK_ERROR
        )
        self.assertEqual(len(server.requests), 1)

    def test_redirect_without_location_is_a_status_failure(self) -> None:
        with LoopbackServer() as server:
            server.route("/", respond_redirect(""))
            result = make_fetcher(server.url("/")).fetch()

        self.assertFalse(result.ok)
        assert result.failure is not None
        self.assertEqual(result.failure.category, FailureCategory.HTTP_STATUS)
        self.assertEqual(len(server.requests), 1)


class FailureTaxonomyTests(unittest.TestCase):
    def test_non_2xx_is_http_status_failure_without_retry(self) -> None:
        with LoopbackServer() as server:
            server.route(
                "/",
                respond_html(b"<html><body>erreur</body></html>", status=503),
            )
            result = make_fetcher(server.url("/")).fetch()

        self.assertFalse(result.ok)
        assert result.failure is not None
        self.assertEqual(result.failure.category, FailureCategory.HTTP_STATUS)
        self.assertEqual(result.http_status, 503)
        self.assertEqual(len(server.requests), 1)

    def test_empty_body_is_rejected(self) -> None:
        with LoopbackServer() as server:
            server.route("/", respond_html(b""))
            result = make_fetcher(server.url("/")).fetch()

        self.assertFalse(result.ok)
        assert result.failure is not None
        self.assertEqual(result.failure.category, FailureCategory.EMPTY_BODY)
        self.assertIsNone(result.snapshot)

    def test_html_without_usable_structure_is_unparseable_content(self) -> None:
        for body in (
            b"<html></html>",
            b"<html><body></body></html>",
            b"<html><body>   \n\t </body></html>",
            b"<html><head><title>titre</title></head></html>",
        ):
            with self.subTest(body=body):
                with LoopbackServer() as server:
                    server.route("/", respond_html(body))
                    result = make_fetcher(server.url("/")).fetch()

                self.assertFalse(result.ok)
                assert result.failure is not None
                self.assertEqual(
                    result.failure.category,
                    FailureCategory.UNPARSEABLE_CONTENT,
                )
                self.assertIsNone(result.snapshot)

    def test_wrong_content_type_is_rejected(self) -> None:
        with LoopbackServer() as server:
            server.route(
                "/",
                respond_html(
                    b'{"not": "html"}',
                    content_type="application/json",
                ),
            )
            result = make_fetcher(server.url("/")).fetch()

        self.assertFalse(result.ok)
        assert result.failure is not None
        self.assertEqual(
            result.failure.category,
            FailureCategory.UNSUPPORTED_CONTENT_TYPE,
        )
        self.assertEqual(len(server.requests), 1)

    def test_missing_content_type_is_rejected(self) -> None:
        with LoopbackServer() as server:
            server.route(
                "/",
                respond_raw(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nabcd"
                ),
            )
            result = make_fetcher(server.url("/")).fetch()

        self.assertFalse(result.ok)
        assert result.failure is not None
        self.assertEqual(
            result.failure.category,
            FailureCategory.UNSUPPORTED_CONTENT_TYPE,
        )

    def test_oversized_response_is_rejected(self) -> None:
        with LoopbackServer() as server:
            server.route("/", respond_html(b"a" * 200))
            result = make_fetcher(
                server.url("/"), max_response_bytes=64
            ).fetch()

        self.assertFalse(result.ok)
        assert result.failure is not None
        self.assertEqual(
            result.failure.category, FailureCategory.OVERSIZED_RESPONSE
        )
        self.assertIsNone(result.snapshot)

    def test_exact_size_limit_is_accepted(self) -> None:
        body = b"<html><body>" + b"a" * 20 + b"</body></html>"
        with LoopbackServer() as server:
            server.route("/", respond_html(body))
            result = make_fetcher(
                server.url("/"), max_response_bytes=len(body)
            ).fetch()

        self.assertTrue(result.ok)

    def test_undecodable_body_is_unparseable_content(self) -> None:
        with LoopbackServer() as server:
            server.route(
                "/",
                respond_html(
                    b"\xff\xff\xff\xff",
                    content_type="text/html; charset=utf-8",
                ),
            )
            result = make_fetcher(server.url("/")).fetch()

        self.assertFalse(result.ok)
        assert result.failure is not None
        self.assertEqual(
            result.failure.category, FailureCategory.UNPARSEABLE_CONTENT
        )
        self.assertIsNone(result.snapshot)

    def test_malformed_http_response_is_a_control_failure(self) -> None:
        with LoopbackServer() as server:
            server.route("/", respond_raw(b"NOT HTTP AT ALL\r\n\r\n"))
            result = make_fetcher(server.url("/")).fetch()

        self.assertFalse(result.ok)
        assert result.failure is not None
        self.assertEqual(
            result.failure.category, FailureCategory.NETWORK_ERROR
        )
        self.assertEqual(len(server.requests), 1)

    def test_connection_refused_is_network_error(self) -> None:
        result = make_fetcher(closed_port_url(), timeout=2.0).fetch()

        self.assertFalse(result.ok)
        assert result.failure is not None
        self.assertEqual(
            result.failure.category, FailureCategory.NETWORK_ERROR
        )
        self.assertIsNone(result.snapshot)

    def test_failure_messages_are_bounded_and_non_empty(self) -> None:
        with LoopbackServer() as server:
            server.route("/", respond_html(b"", status=500))
            result = make_fetcher(server.url("/")).fetch()

        assert result.failure is not None
        self.assertTrue(result.failure.message)
        self.assertLessEqual(len(result.failure.message), 200)


class TimeoutTests(unittest.TestCase):
    def test_timeout_before_headers_is_classified_without_retrying(self) -> None:
        with LoopbackServer() as server:
            server.route("/", respond_after(1.5, b"<html>tard</html>"))
            started = time.monotonic()
            result = make_fetcher(server.url("/"), timeout=0.2).fetch()
            elapsed = time.monotonic() - started

        self.assertFalse(result.ok)
        assert result.failure is not None
        self.assertEqual(result.failure.category, FailureCategory.TIMEOUT)
        self.assertEqual(len(server.requests), 1)
        self.assertLess(elapsed, 0.9)

    def test_slow_drip_body_respects_the_total_timeout(self) -> None:
        # 200 bytes at 20 ms each would take about four seconds without a
        # total-deadline watchdog.
        body = b"x" * 200
        with LoopbackServer() as server:
            server.route("/", respond_drip_body(body, interval=0.02))
            started = time.monotonic()
            result = make_fetcher(server.url("/"), timeout=0.25).fetch()
            elapsed = time.monotonic() - started

        self.assertFalse(result.ok)
        assert result.failure is not None
        self.assertEqual(result.failure.category, FailureCategory.TIMEOUT)
        self.assertEqual(len(server.requests), 1)
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 1.0)

    def test_slow_drip_headers_respects_the_total_timeout(self) -> None:
        raw = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"Content-Length: 2\r\n"
            b"\r\n"
            b"hi"
        )
        with LoopbackServer() as server:
            server.route("/", respond_drip_headers(raw, interval=0.02))
            started = time.monotonic()
            result = make_fetcher(server.url("/"), timeout=0.2).fetch()
            elapsed = time.monotonic() - started

        self.assertFalse(result.ok)
        assert result.failure is not None
        self.assertEqual(result.failure.category, FailureCategory.TIMEOUT)
        self.assertEqual(len(server.requests), 1)
        self.assertLess(elapsed, 0.9)


class IsolationTests(unittest.TestCase):
    def test_links_found_in_the_page_are_not_followed(self) -> None:
        body = (
            b"<html><body><p>Complet 2026</p>"
            b'<a href="/should-not-be-fetched">reserver</a>'
            b"</body></html>"
        )
        with LoopbackServer() as server:
            server.route("/", respond_html(body))
            server.route(
                "/should-not-be-fetched",
                respond_html(b"<html><body>jamais</body></html>"),
            )
            result = make_fetcher(server.url("/")).fetch()

        self.assertTrue(result.ok)
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(server.requests[0].path, "/")

    def test_logs_never_contain_page_content(self) -> None:
        sentinel = "SECRET_PAGE_SENTINEL_6065"
        body = (
            f"<html><body><p>{sentinel}</p></body></html>"
        ).encode("utf-8")
        stream = io.StringIO()
        logger = StructuredLogger(stream)
        with LoopbackServer() as server:
            server.route("/", respond_html(body))
            result = make_fetcher(
                server.url("/"), logger=logger
            ).fetch()

        self.assertTrue(result.ok)
        logs = stream.getvalue()
        self.assertIn("fetch_completed", logs)
        self.assertNotIn(sentinel, logs)
        self.assertNotIn("<html", logs)

    def test_max_redirects_is_bounded(self) -> None:
        config = Config.from_mapping({"target_url": "http://127.0.0.1:1/"})
        with self.assertRaises(ValueError):
            Fetcher(config, max_redirects=MAX_REDIRECTS + 1)
        with self.assertRaises(ValueError):
            Fetcher(config, max_redirects=-1)


if __name__ == "__main__":
    unittest.main()
