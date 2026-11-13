"""Tests fuer trigger_firmware_update/upgrade + fetch_upgrade_status.

Iteration A (v0.11): Single-Device-Firmware-Actions via OPNsense-API.
Deckt Trigger-Erfolgspfad + Fehler-Pfade + Status-Klassifikation ab.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from opn_cockpit.core.device_info import (
    FIRMWARE_UPDATE_ENDPOINT,
    FIRMWARE_UPGRADE_ENDPOINT,
    FIRMWARE_UPGRADE_STATUS_ENDPOINT,
    UPGRADE_STATUS_DONE,
    UPGRADE_STATUS_ERROR,
    UPGRADE_STATUS_RUNNING,
    UPGRADE_STATUS_UNKNOWN,
    fetch_upgrade_status,
    trigger_firmware_update,
    trigger_firmware_upgrade,
)
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning


@dataclass
class _Route:
    path: str
    handler: Callable[[httpx.Request], httpx.Response]


@dataclass
class MockApi:
    routes: list[_Route] = field(default_factory=list)
    received: list[httpx.Request] = field(default_factory=list)

    def on(self, path: str, responder: Callable[[httpx.Request], httpx.Response]) -> MockApi:
        self.routes.append(_Route(path=path, handler=responder))
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.received.append(request)
        for route in self.routes:
            if request.url.path == route.path:
                return route.handler(request)
        return httpx.Response(404, json={"error": "no route: " + request.url.path})


def _build_client(api: MockApi) -> tuple[HttpClient, HttpTarget]:
    transport = httpx.MockTransport(api)
    target = HttpTarget(host="opn-a.lab", port=443, verify=False)
    client = HttpClient(
        targets=[target],
        tuning=HttpTuning(retry_count=0, retry_backoff_base_s=0.0),
        transport=transport,
        sleep=lambda _delay: None,
    )
    return client, target


class TestTriggerUpdate:
    def test_success_returns_ok(self) -> None:
        api = MockApi().on(
            FIRMWARE_UPDATE_ENDPOINT,
            lambda _req: httpx.Response(200, json={"status": "ok"}),
        )
        client, target = _build_client(api)
        ok, msg = trigger_firmware_update(client, target, "k", "s")
        assert ok is True
        assert "Update" in msg

    def test_success_running_status_still_ok(self) -> None:
        # Manche OPNsense-Versionen liefern direkt "running" statt "ok"
        api = MockApi().on(
            FIRMWARE_UPDATE_ENDPOINT,
            lambda _req: httpx.Response(200, json={"status": "running"}),
        )
        client, target = _build_client(api)
        ok, _msg = trigger_firmware_update(client, target, "k", "s")
        assert ok is True

    def test_empty_body_still_ok(self) -> None:
        # OPNsense meldet gelegentlich 200 mit leerem Body
        api = MockApi().on(
            FIRMWARE_UPDATE_ENDPOINT,
            lambda _req: httpx.Response(200, content=b""),
        )
        client, target = _build_client(api)
        ok, _msg = trigger_firmware_update(client, target, "k", "s")
        assert ok is True

    def test_reject_status_returns_false(self) -> None:
        api = MockApi().on(
            FIRMWARE_UPDATE_ENDPOINT,
            lambda _req: httpx.Response(200, json={"status": "denied"}),
        )
        client, target = _build_client(api)
        ok, msg = trigger_firmware_update(client, target, "k", "s")
        assert ok is False
        assert "denied" in msg.lower() or "Update" in msg

    def test_401_maps_to_auth_error(self) -> None:
        api = MockApi().on(
            FIRMWARE_UPDATE_ENDPOINT,
            lambda _req: httpx.Response(401, json={"error": "unauthenticated"}),
        )
        client, target = _build_client(api)
        ok, msg = trigger_firmware_update(client, target, "k", "s")
        assert ok is False
        assert "Auth" in msg


class TestTriggerUpgrade:
    def test_hits_upgrade_endpoint(self) -> None:
        api = MockApi().on(
            FIRMWARE_UPGRADE_ENDPOINT,
            lambda _req: httpx.Response(200, json={"status": "ok"}),
        )
        client, target = _build_client(api)
        ok, msg = trigger_firmware_upgrade(client, target, "k", "s")
        assert ok is True
        assert "Upgrade" in msg
        paths = [r.url.path for r in api.received]
        assert FIRMWARE_UPGRADE_ENDPOINT in paths


class TestFetchUpgradeStatus:
    def test_running(self) -> None:
        api = MockApi().on(
            FIRMWARE_UPGRADE_STATUS_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "status": "running",
                    "log": "step 1\nstep 2\n",
                },
            ),
        )
        client, target = _build_client(api)
        us = fetch_upgrade_status(client, target, "k", "s")
        assert us.reachable is True
        assert us.authenticated is True
        assert us.status == UPGRADE_STATUS_RUNNING
        assert "step 1" in us.log

    def test_done_alias_ok(self) -> None:
        # OPNsense meldet je nach Version 'ok' oder 'done' fuer erfolgreiches
        # Ende - beides muss auf DONE normalisiert werden.
        api = MockApi().on(
            FIRMWARE_UPGRADE_STATUS_ENDPOINT,
            lambda _req: httpx.Response(200, json={"status": "ok", "log": ""}),
        )
        client, target = _build_client(api)
        us = fetch_upgrade_status(client, target, "k", "s")
        assert us.status == UPGRADE_STATUS_DONE

    def test_done_literal(self) -> None:
        api = MockApi().on(
            FIRMWARE_UPGRADE_STATUS_ENDPOINT,
            lambda _req: httpx.Response(200, json={"status": "done", "log": ""}),
        )
        client, target = _build_client(api)
        us = fetch_upgrade_status(client, target, "k", "s")
        assert us.status == UPGRADE_STATUS_DONE

    def test_error(self) -> None:
        api = MockApi().on(
            FIRMWARE_UPGRADE_STATUS_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "status": "error",
                    "log": "pkg fetch failed",
                },
            ),
        )
        client, target = _build_client(api)
        us = fetch_upgrade_status(client, target, "k", "s")
        assert us.status == UPGRADE_STATUS_ERROR
        assert "pkg fetch" in us.log

    def test_unknown_status_word(self) -> None:
        api = MockApi().on(
            FIRMWARE_UPGRADE_STATUS_ENDPOINT,
            lambda _req: httpx.Response(200, json={"status": "gibberish"}),
        )
        client, target = _build_client(api)
        us = fetch_upgrade_status(client, target, "k", "s")
        assert us.status == UPGRADE_STATUS_UNKNOWN
        assert "gibberish" in us.summary

    def test_non_dict_body(self) -> None:
        api = MockApi().on(
            FIRMWARE_UPGRADE_STATUS_ENDPOINT,
            lambda _req: httpx.Response(200, json=[]),
        )
        client, target = _build_client(api)
        us = fetch_upgrade_status(client, target, "k", "s")
        assert us.status == UPGRADE_STATUS_UNKNOWN
        assert us.reachable is True

    def test_401_maps(self) -> None:
        api = MockApi().on(
            FIRMWARE_UPGRADE_STATUS_ENDPOINT,
            lambda _req: httpx.Response(401, json={"error": "auth"}),
        )
        client, target = _build_client(api)
        us = fetch_upgrade_status(client, target, "k", "s")
        assert us.authenticated is False
        assert "Auth" in us.summary
