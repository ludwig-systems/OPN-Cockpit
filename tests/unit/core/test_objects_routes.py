"""Tests für core.objects.routes — RouteAdapter und RoutesController."""

from __future__ import annotations

import json as _json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import pytest

from opn_cockpit.core.errors import (
    AuthError,
    ReconfigureError,
    UnreachableError,
    ValidationError,
)
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning
from opn_cockpit.core.objects._endpoints import (
    ROUTES_ADD,
    ROUTES_RECONFIGURE,
    ROUTES_SEARCH,
)
from opn_cockpit.core.objects.base import DiffKind, RequestContext
from opn_cockpit.core.objects.routes import (
    RouteAdapter,
    RouteIdentity,
    RoutesController,
    RouteSpec,
)

# ---------------------------------------------------------------------------
# Test-Helfer: mehrwege-Router auf Basis von httpx.MockTransport
# ---------------------------------------------------------------------------


@dataclass
class _Route:
    path: str
    handler: Callable[[httpx.Request], httpx.Response]


@dataclass
class MockApi:
    """Sammelt empfangene Requests pro Pfad und liefert konfigurierte Antworten."""

    routes: list[_Route] = field(default_factory=list)
    received: list[httpx.Request] = field(default_factory=list)

    def on(
        self,
        path: str,
        responder: Callable[[httpx.Request], httpx.Response],
    ) -> MockApi:
        self.routes.append(_Route(path=path, handler=responder))
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.received.append(request)
        for route in self.routes:
            if request.url.path == route.path:
                return route.handler(request)
        return httpx.Response(404, json={"error": "no route in MockApi for " + request.url.path})


def _build_client(
    api: MockApi,
    target_host: str = "opn-a.lab",
) -> tuple[HttpClient, RequestContext]:
    transport = httpx.MockTransport(api)
    target = HttpTarget(host=target_host, port=443, verify=False)
    client = HttpClient(
        targets=[target],
        tuning=HttpTuning(retry_count=0, retry_backoff_base_s=0.0),
        transport=transport,
        sleep=lambda _delay: None,
    )
    ctx = RequestContext(target=target, key="key", secret="topsecret")
    return client, ctx


# ---------------------------------------------------------------------------
# Spec / Identity
# ---------------------------------------------------------------------------


class TestSpecs:
    def test_spec_identity_extraction(self) -> None:
        spec = RouteSpec(network="10.0.0.0/24", gateway="WAN_GW", descr="x", disabled=False)
        ident = spec.to_identity()
        assert ident == RouteIdentity(network="10.0.0.0/24", gateway="WAN_GW")

    def test_adapter_identity_helper(self) -> None:
        adapter = RouteAdapter()
        spec = RouteSpec(network="10.0.0.0/24", gateway="WAN_GW")
        assert adapter.identity(spec) == spec.to_identity()


# ---------------------------------------------------------------------------
# to_payload
# ---------------------------------------------------------------------------


class TestToPayload:
    def test_disabled_serialized_as_string_flag(self) -> None:
        adapter = RouteAdapter()
        payload = adapter.to_payload(
            RouteSpec(network="10.0.0.0/24", gateway="WAN_GW", descr="X", disabled=True)
        )
        assert payload == {
            "network": "10.0.0.0/24",
            "gateway": "WAN_GW",
            "descr": "X",
            "disabled": "1",
        }

    def test_enabled_serialized_as_zero(self) -> None:
        adapter = RouteAdapter()
        payload = adapter.to_payload(
            RouteSpec(network="192.168.1.0/24", gateway="LAN_GW", descr="", disabled=False)
        )
        assert payload["disabled"] == "0"


# ---------------------------------------------------------------------------
# exists
# ---------------------------------------------------------------------------


class TestExists:
    def test_returns_none_for_empty_rows(self) -> None:
        api = MockApi().on(
            ROUTES_SEARCH,
            lambda _r: httpx.Response(200, json={"rows": [], "rowCount": 0, "total": 0}),
        )
        client, ctx = _build_client(api)
        adapter = RouteAdapter()
        result = adapter.exists(
            client, ctx, RouteIdentity(network="10.0.0.0/24", gateway="WAN_GW")
        )
        assert result is None

    def test_returns_spec_for_matching_row(self) -> None:
        api = MockApi().on(
            ROUTES_SEARCH,
            lambda _r: httpx.Response(
                200,
                json={
                    "rows": [
                        {
                            "uuid": "abc",
                            "network": "10.0.0.0/24",
                            "gateway": "WAN_GW",
                            "descr": "office",
                            "disabled": "0",
                        }
                    ],
                    "rowCount": 1,
                    "total": 1,
                },
            ),
        )
        client, ctx = _build_client(api)
        adapter = RouteAdapter()
        result = adapter.exists(
            client, ctx, RouteIdentity(network="10.0.0.0/24", gateway="WAN_GW")
        )
        assert result is not None
        assert result.network == "10.0.0.0/24"
        assert result.gateway == "WAN_GW"
        assert result.descr == "office"
        assert result.disabled is False

    def test_does_not_match_on_different_gateway(self) -> None:
        api = MockApi().on(
            ROUTES_SEARCH,
            lambda _r: httpx.Response(
                200,
                json={
                    "rows": [
                        {
                            "network": "10.0.0.0/24",
                            "gateway": "OTHER_GW",
                            "descr": "",
                            "disabled": "0",
                        }
                    ],
                    "rowCount": 1,
                    "total": 1,
                },
            ),
        )
        client, ctx = _build_client(api)
        adapter = RouteAdapter()
        result = adapter.exists(
            client, ctx, RouteIdentity(network="10.0.0.0/24", gateway="WAN_GW")
        )
        assert result is None

    def test_matches_disabled_truthy_values(self) -> None:
        api = MockApi().on(
            ROUTES_SEARCH,
            lambda _r: httpx.Response(
                200,
                json={
                    "rows": [
                        {
                            "network": "10.0.0.0/24",
                            "gateway": "WAN_GW",
                            "descr": "",
                            "disabled": "1",
                        }
                    ],
                    "rowCount": 1,
                    "total": 1,
                },
            ),
        )
        client, ctx = _build_client(api)
        adapter = RouteAdapter()
        result = adapter.exists(
            client, ctx, RouteIdentity(network="10.0.0.0/24", gateway="WAN_GW")
        )
        assert result is not None
        assert result.disabled is True

    def test_cidr_normalization_handles_host_bits(self) -> None:
        # API liefert Eintrag mit Host-Bits zurück (defensiver Vergleich).
        api = MockApi().on(
            ROUTES_SEARCH,
            lambda _r: httpx.Response(
                200,
                json={
                    "rows": [
                        {
                            "network": "10.0.0.5/24",
                            "gateway": "WAN_GW",
                            "descr": "",
                            "disabled": "0",
                        }
                    ],
                    "rowCount": 1,
                    "total": 1,
                },
            ),
        )
        client, ctx = _build_client(api)
        adapter = RouteAdapter()
        result = adapter.exists(
            client, ctx, RouteIdentity(network="10.0.0.0/24", gateway="WAN_GW")
        )
        assert result is not None

    def test_returns_none_for_non_json_response(self) -> None:
        api = MockApi().on(
            ROUTES_SEARCH,
            lambda _r: httpx.Response(200, content=b"not-json"),
        )
        client, ctx = _build_client(api)
        adapter = RouteAdapter()
        result = adapter.exists(
            client, ctx, RouteIdentity(network="10.0.0.0/24", gateway="WAN_GW")
        )
        assert result is None

    def test_returns_none_for_non_dict_response(self) -> None:
        api = MockApi().on(
            ROUTES_SEARCH,
            lambda _r: httpx.Response(200, json=["unexpected", "list"]),
        )
        client, ctx = _build_client(api)
        adapter = RouteAdapter()
        result = adapter.exists(
            client, ctx, RouteIdentity(network="10.0.0.0/24", gateway="WAN_GW")
        )
        assert result is None

    def test_returns_none_when_rows_missing(self) -> None:
        api = MockApi().on(
            ROUTES_SEARCH,
            lambda _r: httpx.Response(200, json={"rowCount": 0, "total": 0}),
        )
        client, ctx = _build_client(api)
        adapter = RouteAdapter()
        result = adapter.exists(
            client, ctx, RouteIdentity(network="10.0.0.0/24", gateway="WAN_GW")
        )
        assert result is None


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


class TestAdd:
    def test_sends_wrapped_payload_and_returns_uuid(self) -> None:
        captured: dict[str, dict[str, str]] = {}

        def add_handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = _json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"result": "saved", "uuid": "uuid-xyz"})

        api = MockApi().on(ROUTES_ADD, add_handler)
        client, ctx = _build_client(api)
        adapter = RouteAdapter()
        outcome = adapter.add(
            client,
            ctx,
            RouteSpec(network="10.0.0.0/24", gateway="WAN_GW", descr="X"),
        )
        assert outcome.uuid == "uuid-xyz"
        assert outcome.raw_status == 200
        # Wrapping in {"route": {...}} und Inner-Felder vollständig.
        assert captured["body"] == {
            "route": {
                "network": "10.0.0.0/24",
                "gateway": "WAN_GW",
                "descr": "X",
                "disabled": "0",
            }
        }

    def test_rejects_invalid_cidr_clientside(self) -> None:
        api = MockApi()
        client, ctx = _build_client(api)
        adapter = RouteAdapter()
        with pytest.raises(ValidationError):
            adapter.add(
                client,
                ctx,
                RouteSpec(network="not-a-cidr", gateway="WAN_GW"),
            )
        # Niemals an die API geschickt.
        assert api.received == []

    def test_rejects_invalid_gateway_name_clientside(self) -> None:
        api = MockApi()
        client, ctx = _build_client(api)
        adapter = RouteAdapter()
        with pytest.raises(ValidationError):
            adapter.add(
                client,
                ctx,
                RouteSpec(network="10.0.0.0/24", gateway="has space"),
            )
        assert api.received == []

    def test_handles_response_without_uuid_field(self) -> None:
        api = MockApi().on(
            ROUTES_ADD,
            lambda _r: httpx.Response(200, json={"result": "saved"}),
        )
        client, ctx = _build_client(api)
        adapter = RouteAdapter()
        outcome = adapter.add(
            client,
            ctx,
            RouteSpec(network="10.0.0.0/24", gateway="WAN_GW"),
        )
        assert outcome.uuid is None

    def test_handles_non_json_response_body(self) -> None:
        api = MockApi().on(
            ROUTES_ADD,
            lambda _r: httpx.Response(200, content=b"OK"),
        )
        client, ctx = _build_client(api)
        adapter = RouteAdapter()
        outcome = adapter.add(
            client,
            ctx,
            RouteSpec(network="10.0.0.0/24", gateway="WAN_GW"),
        )
        assert outcome.uuid is None

    def test_propagates_auth_error_from_http_client(self) -> None:
        api = MockApi().on(
            ROUTES_ADD,
            lambda _r: httpx.Response(401, json={"message": "bad key"}),
        )
        client, ctx = _build_client(api)
        adapter = RouteAdapter()
        with pytest.raises(AuthError):
            adapter.add(
                client,
                ctx,
                RouteSpec(network="10.0.0.0/24", gateway="WAN_GW"),
            )


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


class TestVerify:
    def test_found_after_reconfigure(self) -> None:
        api = MockApi().on(
            ROUTES_SEARCH,
            lambda _r: httpx.Response(
                200,
                json={
                    "rows": [
                        {
                            "network": "10.0.0.0/24",
                            "gateway": "WAN_GW",
                            "descr": "",
                            "disabled": "0",
                        }
                    ],
                    "rowCount": 1,
                    "total": 1,
                },
            ),
        )
        client, ctx = _build_client(api)
        adapter = RouteAdapter()
        outcome = adapter.verify(
            client, ctx, RouteIdentity(network="10.0.0.0/24", gateway="WAN_GW")
        )
        assert outcome.found is True
        assert "10.0.0.0/24" in outcome.detail

    def test_not_found_when_search_empty(self) -> None:
        api = MockApi().on(
            ROUTES_SEARCH,
            lambda _r: httpx.Response(200, json={"rows": [], "rowCount": 0, "total": 0}),
        )
        client, ctx = _build_client(api)
        adapter = RouteAdapter()
        outcome = adapter.verify(
            client, ctx, RouteIdentity(network="10.0.0.0/24", gateway="WAN_GW")
        )
        assert outcome.found is False


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


class TestDiff:
    def test_new_when_current_is_none(self) -> None:
        adapter = RouteAdapter()
        diff = adapter.diff(
            None, RouteSpec(network="10.0.0.0/24", gateway="WAN_GW")
        )
        assert diff.kind is DiffKind.NEW

    def test_skip_when_current_matches_identity(self) -> None:
        adapter = RouteAdapter()
        current = RouteSpec(network="10.0.0.0/24", gateway="WAN_GW")
        target_spec = RouteSpec(network="10.0.0.0/24", gateway="WAN_GW", descr="neu")
        diff = adapter.diff(current, target_spec)
        # Drift in descr wird absichtlich als SKIP geführt — Routen werden in v1
        # nicht in-place aktualisiert.
        assert diff.kind is DiffKind.SKIP


# ---------------------------------------------------------------------------
# RoutesController.reconfigure
# ---------------------------------------------------------------------------


class TestRoutesController:
    def test_calls_reconfigure_endpoint(self) -> None:
        api = MockApi().on(
            ROUTES_RECONFIGURE,
            lambda _r: httpx.Response(200, json={"status": "ok"}),
        )
        client, ctx = _build_client(api)
        controller = RoutesController()
        controller.reconfigure(client, ctx)
        assert any(r.url.path == ROUTES_RECONFIGURE for r in api.received)

    def test_wraps_underlying_auth_error_in_reconfigure_error(self) -> None:
        api = MockApi().on(
            ROUTES_RECONFIGURE,
            lambda _r: httpx.Response(401, json={"message": "nope"}),
        )
        client, ctx = _build_client(api)
        controller = RoutesController()
        with pytest.raises(ReconfigureError) as exc:
            controller.reconfigure(client, ctx)
        assert exc.value.context.error_kind == "reconfigure"
        assert exc.value.context.status_code == 401
        # Ursache bleibt erhalten.
        assert isinstance(exc.value.__cause__, AuthError)

    def test_wraps_unreachable_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("conn refused")

        api = MockApi().on(ROUTES_RECONFIGURE, handler)
        client, ctx = _build_client(api)
        controller = RoutesController()
        with pytest.raises(ReconfigureError) as exc:
            controller.reconfigure(client, ctx)
        assert isinstance(exc.value.__cause__, UnreachableError)
