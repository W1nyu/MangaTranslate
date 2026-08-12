from __future__ import annotations

import http.client
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.translation import KeepAliveHttpClient, ProviderConnectionError


class FakeResponse:
    def __init__(self, status: int, body: bytes, will_close: bool) -> None:
        self.status = status
        self._body = body
        self.will_close = will_close

    def read(self) -> bytes:
        return self._body


class FakeConnection:
    """Records requests and can be told to fail like a dropped keep-alive."""

    def __init__(self, scheme: str, host: str, port: int | None, timeout: float) -> None:
        self.scheme = scheme
        self.host = host
        self.port = port
        self.timeout = timeout
        self.requests: list[tuple[str, str, bytes, dict[str, str]]] = []
        self.closed = False
        self.will_close = False
        self.status = 200
        self.body = b'{"ok":true}'
        self.fail_on_request: Exception | None = None

    def request(self, method: str, target: str, body: bytes, headers: dict[str, str]) -> None:
        if self.fail_on_request is not None:
            error, self.fail_on_request = self.fail_on_request, None
            raise error
        self.requests.append((method, target, body, headers))

    def getresponse(self) -> FakeResponse:
        return FakeResponse(self.status, self.body, self.will_close)

    def close(self) -> None:
        self.closed = True


class RecordingFactory:
    def __init__(self) -> None:
        self.created: list[FakeConnection] = []

    def __call__(self, scheme: str, host: str, port: int | None, timeout: float) -> FakeConnection:
        connection = FakeConnection(scheme, host, port, timeout)
        self.created.append(connection)
        return connection


class KeepAliveHttpClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.factory = RecordingFactory()
        self.client = KeepAliveHttpClient(connection_factory=self.factory, timeout=7)

    def test_the_url_is_split_into_a_request_target(self) -> None:
        status, body = self.client.post(
            "https://translator.example/translate?api-version=3.0&to=ko",
            {"Content-Type": "application/json"},
            b"[]",
        )

        self.assertEqual((status, body), (200, b'{"ok":true}'))
        connection = self.factory.created[0]
        self.assertEqual((connection.scheme, connection.host, connection.port), ("https", "translator.example", None))
        self.assertEqual(connection.timeout, 7)
        method, target, sent, headers = connection.requests[0]
        self.assertEqual((method, target, sent), ("POST", "/translate?api-version=3.0&to=ko", b"[]"))
        self.assertEqual(headers["Connection"], "keep-alive")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_a_path_less_url_still_targets_root(self) -> None:
        self.client.post("https://provider.example", {}, b"{}")
        self.assertEqual(self.factory.created[0].requests[0][1], "/")

    def test_an_explicit_port_and_plain_http_are_honoured(self) -> None:
        self.client.post("http://127.0.0.1:9001/translate", {}, b"{}")
        connection = self.factory.created[0]
        self.assertEqual((connection.scheme, connection.host, connection.port), ("http", "127.0.0.1", 9001))

    def test_the_connection_is_reused_across_requests(self) -> None:
        for _ in range(3):
            self.client.post("https://provider.example/translate", {}, b"{}")

        # This is the whole point: one TLS handshake instead of three.
        self.assertEqual(len(self.factory.created), 1)
        self.assertEqual(len(self.factory.created[0].requests), 3)

    def test_separate_hosts_get_separate_connections(self) -> None:
        self.client.post("https://deepl.example/translate", {}, b"{}")
        self.client.post("https://azure.example/translate", {}, b"{}")
        self.client.post("https://deepl.example/translate", {}, b"{}")

        self.assertEqual(len(self.factory.created), 2)

    def test_a_dropped_keep_alive_connection_is_retried_once(self) -> None:
        self.client.post("https://provider.example/translate", {}, b"{}")
        # The provider closed the idle connection; the failure only surfaces on
        # the next use.
        self.factory.created[0].fail_on_request = http.client.RemoteDisconnected("closed")

        status, _ = self.client.post("https://provider.example/translate", {}, b"{}")

        self.assertEqual(status, 200)
        self.assertEqual(len(self.factory.created), 2, "a fresh connection should have been opened")
        self.assertTrue(self.factory.created[0].closed)
        self.assertEqual(len(self.factory.created[1].requests), 1)

    def test_a_failure_on_a_brand_new_connection_is_not_retried(self) -> None:
        def failing(*args: object) -> FakeConnection:
            connection = self.factory(*args)
            connection.fail_on_request = OSError("no route to host")
            return connection

        client = KeepAliveHttpClient(connection_factory=failing)
        with self.assertRaises(ProviderConnectionError):
            client.post("https://provider.example/translate", {}, b"{}")

        # Retrying an unreachable provider only delays the fallback provider.
        self.assertEqual(len(self.factory.created), 1)

    def test_a_connection_the_server_will_close_is_not_pooled(self) -> None:
        def closing(*args: object) -> FakeConnection:
            connection = self.factory(*args)
            # `Connection: close` in the provider's response.
            connection.will_close = True
            return connection

        client = KeepAliveHttpClient(connection_factory=closing)
        client.post("https://provider.example/translate", {}, b"{}")
        client.post("https://provider.example/translate", {}, b"{}")

        self.assertEqual(len(self.factory.created), 2, "a connection the server closes must not be pooled")
        self.assertTrue(self.factory.created[0].closed)

    def test_error_statuses_are_returned_rather_than_raised(self) -> None:
        self.client.post("https://provider.example/translate", {}, b"{}")
        self.factory.created[0].status = 429
        self.factory.created[0].body = b'{"message":"slow down"}'

        status, body = self.client.post("https://provider.example/translate", {}, b"{}")

        self.assertEqual(status, 429)
        self.assertEqual(body, b'{"message":"slow down"}')

    def test_a_malformed_url_is_rejected(self) -> None:
        with self.assertRaises(ProviderConnectionError):
            self.client.post("not-a-url", {}, b"{}")

    def test_close_drops_every_pooled_connection(self) -> None:
        self.client.post("https://deepl.example/translate", {}, b"{}")
        self.client.post("https://azure.example/translate", {}, b"{}")

        self.client.close()

        self.assertTrue(all(connection.closed for connection in self.factory.created))
        self.client.post("https://deepl.example/translate", {}, b"{}")
        self.assertEqual(len(self.factory.created), 3)


if __name__ == "__main__":
    unittest.main()
