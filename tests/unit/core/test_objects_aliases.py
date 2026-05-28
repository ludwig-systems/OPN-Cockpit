"""Tests für core.objects.aliases — AliasAdapter (create + append-Merge)."""

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
    ALIAS_ADD,
    ALIAS_GET,
    ALIAS_RECONFIGURE,
    ALIAS_SEARCH,
    ALIAS_SET,
)
from opn_cockpit.core.objects.aliases import (
    AliasAdapter,
    AliasesController,
    AliasIdentity,
    AliasSpec,
)
from opn_cockpit.core.objects.base import DiffKind, RequestContext

# ---------------------------------------------------------------------------
# MockApi-Helfer (analog routes-Tests)
# ---------------------------------------------------------------------------


@dataclass
class _Route:
    method: str
    path: str
    handler: Callable[[httpx.Request], httpx.Response]


@dataclass
class MockApi:
    routes: list[_Route] = field(default_factory=list)
    received: list[httpx.Request] = field(default_factory=list)

    def on(
        self,
        method: str,
        path: str,
        responder: Callable[[httpx.Request], httpx.Response],
    ) -> MockApi:
        self.routes.append(_Route(method=method, path=path, handler=responder))
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.received.append(request)
        for route in self.routes:
            # Pfade mit {uuid} matchen wir per Präfix
            base_path = route.path.split("{")[0]
            if request.method == route.method and request.url.path.startswith(base_path):
                return route.handler(request)
        body = {"error": f"no route for {request.method} {request.url.path}"}
        return httpx.Response(404, json=body)


def _build_client(api: MockApi) -> tuple[HttpClient, RequestContext]:
    transport = httpx.MockTransport(api)
    target = HttpTarget(host="opn-a.lab", port=443, verify=False)
    client = HttpClient(
        targets=[target],
        tuning=HttpTuning(retry_count=0),
        transport=transport,
        sleep=lambda _delay: None,
    )
    ctx = RequestContext(target=target, key="key", secret="topsecret")
    return client, ctx


# ---------------------------------------------------------------------------
# Spec + Serialisierung
# ---------------------------------------------------------------------------


class TestSpec:
    def test_identity(self) -> None:
        spec = AliasSpec(name="branch_ips", type="host", content=("1.1.1.1",))
        assert spec.to_identity() == AliasIdentity(name="branch_ips")

    def test_spec_dict_roundtrip(self) -> None:
        adapter = AliasAdapter()
        original = AliasSpec(
            name="ips", type="host", content=("a", "b"),
            descr="x", merge_mode="append",
        )
        restored = adapter.spec_from_dict(adapter.spec_to_dict(original))
        assert restored == original

    def test_spec_from_dict_normalizes_string_content(self) -> None:
        # Wenn Tools versehentlich content als String übergeben, akzeptieren wir
        # Komma-separierte Werte defensiv.
        adapter = AliasAdapter()
        restored = adapter.spec_from_dict(
            {"name": "x", "type": "host", "content": "1.1.1.1, 2.2.2.2"}
        )
        assert restored.content == ("1.1.1.1", "2.2.2.2")


class TestToPayload:
    def test_content_serialized_newline_separated(self) -> None:
        adapter = AliasAdapter()
        payload = adapter.to_payload(
            AliasSpec(name="x", type="host", content=("a", "b", "c"))
        )
        assert payload == {
            "name": "x",
            "type": "host",
            "content": "a\nb\nc",
            "description": "",
        }


# ---------------------------------------------------------------------------
# exists
# ---------------------------------------------------------------------------


class TestExists:
    def test_returns_none_when_not_found(self) -> None:
        api = MockApi().on(
            "POST", ALIAS_SEARCH,
            lambda _r: httpx.Response(200, json={"rows": []}),
        )
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        assert adapter.exists(client, ctx, AliasIdentity(name="missing")) is None

    def test_returns_spec_when_found(self) -> None:
        def search(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"rows": [{"uuid": "uuid-1", "name": "branch_ips"}]},
            )

        def get(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "alias": {
                        "name": "branch_ips",
                        "type": "host",
                        "content": "10.0.0.1\n10.0.0.2",
                        "description": "Zweigstellen",
                    }
                },
            )

        api = MockApi().on("POST", ALIAS_SEARCH, search).on("GET", ALIAS_GET, get)
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        result = adapter.exists(client, ctx, AliasIdentity(name="branch_ips"))
        assert result is not None
        assert result.name == "branch_ips"
        assert result.type == "host"
        assert result.content == ("10.0.0.1", "10.0.0.2")
        assert result.descr == "Zweigstellen"

    def test_only_exact_name_match(self) -> None:
        api = MockApi().on(
            "POST", ALIAS_SEARCH,
            lambda _r: httpx.Response(
                200,
                json={"rows": [{"uuid": "u1", "name": "branch_ips_lab"}]},
            ),
        )
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        assert adapter.exists(client, ctx, AliasIdentity(name="branch_ips")) is None


# ---------------------------------------------------------------------------
# add (create mode)
# ---------------------------------------------------------------------------


class TestCreate:
    def test_sends_add_item_with_wrapped_payload(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = _json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"result": "saved", "uuid": "new-uuid"})

        api = MockApi().on("POST", ALIAS_ADD, handler)
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        outcome = adapter.add(
            client, ctx,
            AliasSpec(name="ips", type="host", content=("1.1.1.1",), descr="x"),
        )
        assert outcome.uuid == "new-uuid"
        assert captured["body"] == {
            "alias": {
                "name": "ips",
                "type": "host",
                "content": "1.1.1.1",
                "description": "x",
            }
        }

    def test_rejects_invalid_name(self) -> None:
        api = MockApi()
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        with pytest.raises(ValidationError):
            adapter.add(client, ctx, AliasSpec(name="has space", type="host", content=("a",)))
        # Nichts an die API geschickt.
        assert api.received == []

    def test_rejects_unknown_type(self) -> None:
        api = MockApi()
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        with pytest.raises(ValidationError):
            adapter.add(client, ctx, AliasSpec(name="ok", type="unknown", content=("a",)))

    def test_propagates_auth_error(self) -> None:
        api = MockApi().on(
            "POST", ALIAS_ADD,
            lambda _r: httpx.Response(401, json={"message": "nope"}),
        )
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        with pytest.raises(AuthError):
            adapter.add(
                client, ctx,
                AliasSpec(name="ips", type="host", content=("1.1.1.1",)),
            )


# ---------------------------------------------------------------------------
# add (append mode / Merge)
# ---------------------------------------------------------------------------


class TestAppend:
    def test_append_to_existing_alias(self) -> None:
        captured: dict[str, object] = {}

        def search(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"rows": [{"uuid": "u1", "name": "ips"}]})

        def get(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "alias": {
                        "name": "ips",
                        "type": "host",
                        "content": "1.1.1.1\n2.2.2.2",
                        "description": "alt",
                    }
                },
            )

        def setitem(request: httpx.Request) -> httpx.Response:
            captured["body"] = _json.loads(request.content.decode("utf-8"))
            captured["path"] = request.url.path
            return httpx.Response(200, json={"result": "saved"})

        api = (
            MockApi()
            .on("POST", ALIAS_SEARCH, search)
            .on("GET", ALIAS_GET, get)
            .on("POST", ALIAS_SET.split("{")[0], setitem)
        )
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        outcome = adapter.add(
            client, ctx,
            AliasSpec(
                name="ips", type="host",
                content=("2.2.2.2", "3.3.3.3"),  # 2.2 doppelt, 3.3 neu
                merge_mode="append",
            ),
        )
        assert outcome.uuid == "u1"
        # set-Pfad enthält die uuid
        assert "u1" in str(captured["path"])
        # Inhalt: erst bestehende, dann nur die fehlenden — deduplikatfrei
        sent = captured["body"]
        assert isinstance(sent, dict)
        alias = sent["alias"]
        assert isinstance(alias, dict)
        assert alias["content"] == "1.1.1.1\n2.2.2.2\n3.3.3.3"

    def test_append_fails_if_alias_missing(self) -> None:
        api = MockApi().on(
            "POST", ALIAS_SEARCH,
            lambda _r: httpx.Response(200, json={"rows": []}),
        )
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        with pytest.raises(ValidationError) as exc:
            adapter.add(
                client, ctx,
                AliasSpec(name="missing", type="host", content=("x",), merge_mode="append"),
            )
        assert exc.value.context.error_kind == "alias_not_found"


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


class TestDiff:
    def test_new_when_no_current(self) -> None:
        adapter = AliasAdapter()
        d = adapter.diff(None, AliasSpec(name="x", type="host", content=("a",)))
        assert d.kind is DiffKind.NEW

    def test_new_with_warning_for_append_when_missing(self) -> None:
        adapter = AliasAdapter()
        d = adapter.diff(
            None,
            AliasSpec(name="x", type="host", content=("a",), merge_mode="append"),
        )
        assert d.kind is DiffKind.NEW
        assert "Append" in d.summary or "append" in d.summary

    def test_skip_when_create_identical(self) -> None:
        adapter = AliasAdapter()
        current = AliasSpec(name="x", type="host", content=("a", "b"))
        target_spec = AliasSpec(name="x", type="host", content=("b", "a"))
        d = adapter.diff(current, target_spec)
        # Reihenfolge irrelevant — Mengen-Vergleich
        assert d.kind is DiffKind.SKIP

    def test_update_warning_for_create_conflict(self) -> None:
        adapter = AliasAdapter()
        current = AliasSpec(name="x", type="host", content=("a",))
        target_spec = AliasSpec(name="x", type="host", content=("b",))
        d = adapter.diff(current, target_spec)
        assert d.kind is DiffKind.UPDATE

    def test_skip_when_append_all_already_present(self) -> None:
        adapter = AliasAdapter()
        current = AliasSpec(name="x", type="host", content=("a", "b", "c"))
        target_spec = AliasSpec(
            name="x", type="host", content=("a", "b"), merge_mode="append",
        )
        d = adapter.diff(current, target_spec)
        assert d.kind is DiffKind.SKIP

    def test_update_when_append_adds_new(self) -> None:
        adapter = AliasAdapter()
        current = AliasSpec(name="x", type="host", content=("a",))
        target_spec = AliasSpec(
            name="x", type="host", content=("a", "b", "c"), merge_mode="append",
        )
        d = adapter.diff(current, target_spec)
        assert d.kind is DiffKind.UPDATE
        assert "+2" in d.summary


# ---------------------------------------------------------------------------
# AliasesController.reconfigure
# ---------------------------------------------------------------------------


class TestAliasesController:
    def test_calls_reconfigure(self) -> None:
        api = MockApi().on(
            "POST", ALIAS_RECONFIGURE,
            lambda _r: httpx.Response(200, json={"status": "ok"}),
        )
        client, ctx = _build_client(api)
        AliasesController().reconfigure(client, ctx)
        assert any(r.url.path == ALIAS_RECONFIGURE for r in api.received)

    def test_wraps_auth_error(self) -> None:
        api = MockApi().on(
            "POST", ALIAS_RECONFIGURE,
            lambda _r: httpx.Response(401, json={"message": "nope"}),
        )
        client, ctx = _build_client(api)
        with pytest.raises(ReconfigureError) as exc:
            AliasesController().reconfigure(client, ctx)
        assert isinstance(exc.value.__cause__, AuthError)

    def test_wraps_unreachable(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline")

        api = MockApi().on("POST", ALIAS_RECONFIGURE, handler)
        client, ctx = _build_client(api)
        with pytest.raises(ReconfigureError) as exc:
            AliasesController().reconfigure(client, ctx)
        assert isinstance(exc.value.__cause__, UnreachableError)
