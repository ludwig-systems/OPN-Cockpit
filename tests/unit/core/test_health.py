"""Tests für core.health — check_device."""

from __future__ import annotations

import httpx

from opn_cockpit.core.health import HEALTH_ENDPOINT, HealthResult, check_device
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
