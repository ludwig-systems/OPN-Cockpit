"""Tests für core.health — check_device + tcp_probe."""

from __future__ import annotations

import socket
import threading

import httpx

from opn_cockpit.core.health import HEALTH_ENDPOINT, HealthResult, check_device, tcp_probe
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning


def _client_with_handler(handler) -> tuple[HttpClient, HttpTarget]:  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)
    target = HttpTarget(host="opn-lab", port=443, verify=False)
    client = HttpClient(
        targets=[target],
        tuning=HttpTuning(retry_count=0),
        transport=transport,
        sleep=lambda _delay: None,
    )
    return client, target


class TestCheckDevice:
    def test_returns_ok_on_200(self) -> None:
        def ok(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"some": "menu"})

        client, target = _client_with_handler(ok)
        result = check_device(client, target, "k", "s")
        assert isinstance(result, HealthResult)
        assert result.is_ok
        assert result.reachable
        assert result.authenticated

    def test_401_results_in_no_auth(self) -> None:
        def auth_fail(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "nope"})

        client, target = _client_with_handler(auth_fail)
        result = check_device(client, target, "k", "s")
        assert result.reachable
        assert not result.authenticated
        assert not result.is_ok

    def test_network_failure_results_in_unreachable(self) -> None:
        def boom(_req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline")

        client, target = _client_with_handler(boom)
        result = check_device(client, target, "k", "s")
        assert not result.reachable
        assert not result.authenticated

    def test_5xx_results_in_no_auth_safely(self) -> None:
        def fail(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "internal"})

        client, target = _client_with_handler(fail)
        result = check_device(client, target, "k", "s")
        # Wir sind nicht "OK", aber das Gerät ist erreichbar.
        assert result.reachable
        assert not result.authenticated

    def test_uses_correct_endpoint(self) -> None:
        seen: list[str] = []

        def capture(req: httpx.Request) -> httpx.Response:
            seen.append(req.url.path)
            return httpx.Response(200, json={})

        client, target = _client_with_handler(capture)
        check_device(client, target, "k", "s")
        assert seen == [HEALTH_ENDPOINT]


class TestTcpProbe:
    """Tests fuer den TCP-Connect-Probe ohne HTTP/Auth.

    Wir spinnen einen lokalen Listener auf 127.0.0.1 hoch, damit der Probe
    nicht aufs externe Netzwerk angewiesen ist.
    """

    def test_returns_true_for_open_port(self) -> None:
        # Ephemeral Listener
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        # In Background-Thread accept() aufrufen, damit der Probe sauber
        # zurueckkommen kann.
        def _accept_and_close() -> None:
            conn, _ = srv.accept()
            conn.close()

        accept_thread = threading.Thread(target=_accept_and_close)
        accept_thread.start()
        try:
            assert tcp_probe("127.0.0.1", port, timeout_s=2.0) is True
        finally:
            accept_thread.join(timeout=2.0)
            srv.close()

    def test_returns_false_for_closed_port(self) -> None:
        # Wir nehmen Port 1 (privilegiert + per Default nicht offen)
        # auf einer reservierten Adresse — fast garantiert geschlossen.
        # Falls auf irgendeinem CI-Runner Port 1 doch offen sein sollte,
        # ist 65534 als Fallback unwahrscheinlich.
        assert tcp_probe("127.0.0.1", 1, timeout_s=1.0) is False

    def test_returns_false_for_unreachable_host(self) -> None:
        # 240.0.0.0/4 ist reserviert + nicht-routbar — Connect-Versuch
        # läuft in Timeout.
        assert tcp_probe("240.0.0.1", 443, timeout_s=0.5) is False

    def test_returns_false_for_dns_resolution_failure(self) -> None:
        # Garantiert nicht aufloesbarer Hostname
        assert tcp_probe("nonexistent-host-xyz.invalid", 443, timeout_s=1.0) is False

    def test_does_not_raise_on_any_error(self) -> None:
        # Heartbeat soll robust sein — kein Aufrufer will Exceptions
        # behandeln muessen.
        tcp_probe("", 443, timeout_s=0.5)
        tcp_probe("not a host name", -1, timeout_s=0.5)
