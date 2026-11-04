from __future__ import annotations

import json
import unittest
from unittest import mock

from app.config import ConfigError, SmsConfig
from app.sms_provider import (
    HttpJsonSmsProvider,
    HttpRequest,
    HttpResponse,
    SmsProvider,
    SmsProviderError,
    load_sms_provider,
    urllib_transport,
)


def sms_config(**overrides) -> SmsConfig:
    values = {
        "provider": "free-sms",
        "url": "https://sms.example.org/send",
        "method": "POST",
        "auth_mode": "bearer",
        "sender": "Alertes",
        "recipient": "+33612345678",
        "credential": "credential-secret",
        "timeout_seconds": 4.0,
        "max_response_bytes": 4096,
    }
    values.update(overrides)
    return SmsConfig(**values)


class RecordingTransport:
    def __init__(self, response: HttpResponse | None = None, error=None) -> None:
        self.requests: list[HttpRequest] = []
        self.response = response or HttpResponse(200, b"{}")
        self.error = error

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


class SmsProviderLoadingTests(unittest.TestCase):
    def test_no_provider_is_selected_by_default(self) -> None:
        self.assertIsNone(load_sms_provider(SmsConfig()))

    def test_incomplete_configuration_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            HttpJsonSmsProvider(sms_config(provider=""))

    def test_construction_does_not_touch_the_network(self) -> None:
        with mock.patch("app.sms_provider.urlopen") as urlopen:
            provider = load_sms_provider(sms_config())
            self.assertIsNotNone(provider)

        urlopen.assert_not_called()

    def test_provider_can_be_swapped_through_configuration(self) -> None:
        first_transport = RecordingTransport(HttpResponse(200, b'{"ok":true}'))
        second_transport = RecordingTransport(HttpResponse(201, b'{"ok":true}'))

        first = load_sms_provider(
            sms_config(provider="provider-a", url="https://a.example.org/send"),
            transport=first_transport,
        )
        second = load_sms_provider(
            sms_config(provider="provider-b", url="https://b.example.org/send"),
            transport=second_transport,
        )

        self.assertIsInstance(first, SmsProvider)
        self.assertIsInstance(second, SmsProvider)
        first.send("Message")
        second.send("Message")

        self.assertEqual(first_transport.requests[0].url, "https://a.example.org/send")
        self.assertEqual(second_transport.requests[0].url, "https://b.example.org/send")


class HttpJsonSmsProviderTests(unittest.TestCase):
    def test_default_template_renders_the_message(self) -> None:
        transport = RecordingTransport(HttpResponse(200, b'{"ok":true}'))
        provider = HttpJsonSmsProvider(sms_config(), transport=transport)

        result = provider.send("Page modifiée https://example.org/p/1")

        request = transport.requests[0]
        payload = json.loads(request.body.decode("utf-8"))
        self.assertEqual(payload["to"], "+33612345678")
        self.assertEqual(payload["sender"], "Alertes")
        self.assertEqual(
            payload["message"], "Page modifiée https://example.org/p/1"
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.timeout, 4.0)
        self.assertEqual(request.max_response_bytes, 4096)
        self.assertEqual(result.provider, "free-sms")
        self.assertEqual(result.status_code, 200)

    def test_bearer_header_authentication(self) -> None:
        transport = RecordingTransport()
        provider = HttpJsonSmsProvider(sms_config(), transport=transport)

        provider.send("Message")

        headers = dict(transport.requests[0].headers)
        self.assertEqual(headers["Authorization"], "Bearer credential-secret")

    def test_custom_header_authentication(self) -> None:
        transport = RecordingTransport()
        provider = HttpJsonSmsProvider(
            sms_config(auth_mode="header", auth_header="X-Api-Key"),
            transport=transport,
        )

        provider.send("Message")

        headers = dict(transport.requests[0].headers)
        self.assertEqual(headers["X-Api-Key"], "credential-secret")

    def test_query_authentication(self) -> None:
        transport = RecordingTransport()
        provider = HttpJsonSmsProvider(
            sms_config(
                auth_mode="query",
                auth_query_param="api_key",
                url="https://sms.example.org/send?lang=fr",
            ),
            transport=transport,
        )

        provider.send("Message")

        self.assertIn("lang=fr", transport.requests[0].url)
        self.assertIn("api_key=credential-secret", transport.requests[0].url)

    def test_custom_body_template_is_rendered_as_json(self) -> None:
        transport = RecordingTransport()
        provider = HttpJsonSmsProvider(
            sms_config(
                body_template='{"recipient": {"number": "{{to}}"}, "text": "{{message}}"}'
            ),
            transport=transport,
        )

        provider.send('Message "quoted"')

        payload = json.loads(transport.requests[0].body.decode("utf-8"))
        self.assertEqual(payload["recipient"]["number"], "+33612345678")
        self.assertEqual(payload["text"], 'Message "quoted"')

    def test_success_predicate_accepts_expected_value(self) -> None:
        transport = RecordingTransport(
            HttpResponse(200, b'{"status": "sent", "nested": {"ok": true}}')
        )
        provider = HttpJsonSmsProvider(
            sms_config(
                success_json_field="nested.ok",
                success_json_value="true",
            ),
            transport=transport,
        )

        result = provider.send("Message")

        self.assertEqual(result.status_code, 200)

    def test_success_predicate_rejects_unconfirmed_send(self) -> None:
        transport = RecordingTransport(HttpResponse(200, b'{"status": "failed"}'))
        provider = HttpJsonSmsProvider(
            sms_config(success_json_field="status", success_json_value="sent"),
            transport=transport,
        )

        with self.assertRaises(SmsProviderError) as raised:
            provider.send("Message")

        self.assertEqual(raised.exception.code, "sms_rejected")

    def test_success_predicate_rejects_missing_field_and_invalid_json(self) -> None:
        missing = HttpJsonSmsProvider(
            sms_config(success_json_field="status"),
            transport=RecordingTransport(HttpResponse(200, b'{"other": true}')),
        )
        invalid = HttpJsonSmsProvider(
            sms_config(success_json_field="status"),
            transport=RecordingTransport(HttpResponse(200, b"not-json")),
        )

        with self.assertRaises(SmsProviderError) as missing_error:
            missing.send("Message")
        with self.assertRaises(SmsProviderError) as invalid_error:
            invalid.send("Message")

        self.assertEqual(missing_error.exception.code, "sms_rejected")
        self.assertEqual(invalid_error.exception.code, "sms_invalid_response")

    def test_http_error_is_redacted_and_bounded(self) -> None:
        body = (
            '{"error": "credential-secret +33612345678"}'
            + "x" * 1000
        ).encode("utf-8")
        provider = HttpJsonSmsProvider(
            sms_config(),
            transport=RecordingTransport(HttpResponse(503, body)),
        )

        with self.assertRaises(SmsProviderError) as raised:
            provider.send("Message")

        error = raised.exception
        self.assertEqual(error.code, "sms_http_error")
        self.assertIn("HTTP 503", error.detail)
        self.assertNotIn("credential-secret", error.detail)
        self.assertNotIn("+33612345678", error.detail)
        self.assertLessEqual(len(error.detail), 500)

    def test_network_and_timeout_errors_are_distinct(self) -> None:
        network = HttpJsonSmsProvider(
            sms_config(),
            transport=RecordingTransport(error=OSError("connection refused")),
        )
        timeout = HttpJsonSmsProvider(
            sms_config(),
            transport=RecordingTransport(error=TimeoutError()),
        )

        with self.assertRaises(SmsProviderError) as network_error:
            network.send("Message")
        with self.assertRaises(SmsProviderError) as timeout_error:
            timeout.send("Message")

        self.assertEqual(network_error.exception.code, "sms_network_error")
        self.assertNotIn("credential-secret", network_error.exception.detail)
        self.assertEqual(timeout_error.exception.code, "sms_timeout")

    def test_empty_message_is_rejected_without_calling_the_provider(self) -> None:
        transport = RecordingTransport()
        provider = HttpJsonSmsProvider(sms_config(), transport=transport)

        with self.assertRaises(SmsProviderError) as raised:
            provider.send("   ")

        self.assertEqual(raised.exception.code, "sms_empty_message")
        self.assertEqual(transport.requests, [])

    def test_response_is_bounded(self) -> None:
        transport = RecordingTransport(HttpResponse(500, b"x" * 5000))
        provider = HttpJsonSmsProvider(
            sms_config(max_response_bytes=300),
            transport=transport,
        )

        with self.assertRaises(SmsProviderError) as raised:
            provider.send("Message")

        self.assertLessEqual(len(raised.exception.detail), 500)
        self.assertLessEqual(transport.requests[0].max_response_bytes, 300)


class UrllibTransportTests(unittest.TestCase):
    def test_response_read_is_bounded(self) -> None:
        class FakeResponse:
            status = 200

            def __init__(self) -> None:
                self.read_size = None

            def read(self, size):
                self.read_size = size
                return b"x" * size

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        response = FakeResponse()
        request = HttpRequest(
            method="POST",
            url="https://sms.example.org/send",
            headers=(),
            body=b"{}",
            timeout=1.0,
            max_response_bytes=16,
        )

        with mock.patch("app.sms_provider.urlopen", return_value=response):
            result = urllib_transport(request)

        self.assertEqual(response.read_size, 17)
        self.assertEqual(len(result.body), 16)


if __name__ == "__main__":
    unittest.main()
