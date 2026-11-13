"""Tests für core.objects.aliases — AliasAdapter (create + append-Merge)."""

from __future__ import annotations

import json as _json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

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
            name="ips",
            type="host",
            content=("a", "b"),
            descr="x",
            merge_mode="append",
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

    def test_spec_dict_roundtrip_preserves_replace(self) -> None:
        # Regressions-Test: bei einem persistierten Sync-Plan (PlanStore
        # ruft spec_to_dict/spec_from_dict) darf der replace-Modus
        # NICHT verloren gehen und still auf 'create' faellen. Das war
        # der Bug in der ersten Iteration des Sync-Fix.
        adapter = AliasAdapter()
        original = AliasSpec(
            name="ips",
            type="host",
            content=("a", "b"),
            descr="",
            merge_mode="replace",
        )
        restored = adapter.spec_from_dict(adapter.spec_to_dict(original))
        assert restored.merge_mode == "replace"

    def test_spec_from_dict_unknown_mode_falls_back_to_create(self) -> None:
        # Defensiv: unbekannte merge_mode-Werte werden zu 'create'.
        # Damit koennen aeltere Cockpit-Versionen einen Plan aus neuerem
        # Cockpit lesen ohne zu crashen — sie machen halt Create.
        adapter = AliasAdapter()
        restored = adapter.spec_from_dict(
            {"name": "x", "type": "host", "content": ["a"], "merge_mode": "future_mode_v3"}
        )
        assert restored.merge_mode == "create"


class TestToPayload:
    def test_content_serialized_newline_separated(self) -> None:
        adapter = AliasAdapter()
        payload = adapter.to_payload(AliasSpec(name="x", type="host", content=("a", "b", "c")))
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
            "POST",
            ALIAS_SEARCH,
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
            "POST",
            ALIAS_SEARCH,
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
            client,
            ctx,
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
            "POST",
            ALIAS_ADD,
            lambda _r: httpx.Response(401, json={"message": "nope"}),
        )
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        with pytest.raises(AuthError):
            adapter.add(
                client,
                ctx,
                AliasSpec(name="ips", type="host", content=("1.1.1.1",)),
            )

    def test_create_raises_when_addItem_returns_failed_result(self) -> None:
        """F18 (create-Pfad): wie beim Append darf 200 + failed nicht als
        Erfolg gewertet werden."""
        from opn_cockpit.core.errors import ApiError

        api = MockApi().on(
            "POST",
            ALIAS_ADD,
            lambda _r: httpx.Response(
                200,
                json={
                    "result": "failed",
                    "validations": {"alias.name": "Already exists"},
                },
            ),
        )
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        with pytest.raises(ApiError) as exc:
            adapter.add(
                client,
                ctx,
                AliasSpec(name="dup", type="host", content=("1.1.1.1",)),
            )
        assert exc.value.context.error_kind == "opnsense_save_failed"


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
            client,
            ctx,
            AliasSpec(
                name="ips",
                type="host",
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
            "POST",
            ALIAS_SEARCH,
            lambda _r: httpx.Response(200, json={"rows": []}),
        )
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        with pytest.raises(ValidationError) as exc:
            adapter.add(
                client,
                ctx,
                AliasSpec(name="missing", type="host", content=("x",), merge_mode="append"),
            )
        assert exc.value.context.error_kind == "alias_not_found"

    def test_append_decodes_opnsense_select_type_dict(self) -> None:
        """F18 Doppelbug: OPNsense ``getItem`` liefert ``type`` und
        ``content`` als Select-Map. _row_to_spec muss den selected-Key
        extrahieren — sonst senden wir an setItem ``type=\"{host: {value:...\"``
        und der Append scheitert mit ``alias.type: Invalid``."""
        captured: dict[str, Any] = {}

        def search(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"rows": [{"uuid": "u1", "name": "ips"}]})

        # Realistische OPNsense-getItem-Antwort: type und content als Map
        # mit selected-Flag.
        def get(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "alias": {
                        "name": "ips",
                        "type": {
                            "host": {"value": "Host(s)", "selected": 0},
                            "network": {"value": "Network(s)", "selected": 1},
                            "port": {"value": "Port(s)", "selected": 0},
                        },
                        "content": {
                            "10.0.0.0/24": {"value": "10.0.0.0/24", "selected": 1},
                        },
                        "description": "Vorhandener Network-Alias",
                    }
                },
            )

        def setitem(request: httpx.Request) -> httpx.Response:
            captured["body"] = _json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"result": "saved"})

        api = (
            MockApi()
            .on("POST", ALIAS_SEARCH, search)
            .on("GET", ALIAS_GET, get)
            .on("POST", ALIAS_SET.split("{")[0], setitem)
        )
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        adapter.add(
            client,
            ctx,
            AliasSpec(
                name="ips",
                type="host",  # User-Eingabe — wird ignoriert, Bestand gilt
                content=("10.99.0.0/24",),
                merge_mode="append",
            ),
        )
        # Wichtig: setItem-Payload muss type=\"network\" enthalten (Bestand),
        # NICHT die str-Repraesentation des Dicts.
        alias_payload = captured["body"]["alias"]
        assert alias_payload["type"] == "network", (
            f"type sollte 'network' sein (selected im getItem-Dict), war: {alias_payload['type']!r}"
        )
        # content gemerged: bestehendes 10.0.0.0/24 plus neues 10.99.0.0/24
        assert alias_payload["content"] == "10.0.0.0/24\n10.99.0.0/24"

    def test_append_raises_when_setitem_returns_failed_result(self) -> None:
        """F18: 200 OK + ``{'result':'failed', 'validations':{...}}`` darf
        nicht mehr als Erfolg gelten — der Eintrag wurde nicht uebernommen."""
        from opn_cockpit.core.errors import ApiError

        def search(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"rows": [{"uuid": "u1", "name": "ips"}]})

        def get(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"alias": {"name": "ips", "type": "host", "content": "1.1.1.1"}},
            )

        def setitem(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "result": "failed",
                    "validations": {"alias.content": "Invalid content"},
                },
            )

        api = (
            MockApi()
            .on("POST", ALIAS_SEARCH, search)
            .on("GET", ALIAS_GET, get)
            .on("POST", ALIAS_SET.split("{")[0], setitem)
        )
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        with pytest.raises(ApiError) as exc:
            adapter.add(
                client,
                ctx,
                AliasSpec(
                    name="ips",
                    type="host",
                    content=("not-a-host-name!!",),
                    merge_mode="append",
                ),
            )
        assert exc.value.context.error_kind == "opnsense_save_failed"
        assert "alias.content" in str(exc.value)


# ---------------------------------------------------------------------------
# add(merge_mode="replace")  — Sync/Upsert-Pfad
# ---------------------------------------------------------------------------


class TestReplace:
    def test_replace_creates_when_missing(self) -> None:
        """Target hat den Alias nicht -> Adapter faellt auf _create zurueck,
        d.h. addItem statt setItem."""
        captured: dict[str, object] = {}

        def search(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"rows": []})

        def additem(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = _json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"result": "saved", "uuid": "u-new"})

        api = MockApi().on("POST", ALIAS_SEARCH, search).on("POST", ALIAS_ADD, additem)
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        outcome = adapter.add(
            client,
            ctx,
            AliasSpec(name="new", type="host", content=("1.1.1.1",), merge_mode="replace"),
        )
        assert outcome.uuid == "u-new"
        assert str(captured["path"]).endswith(ALIAS_ADD)

    def test_replace_sets_when_exists(self) -> None:
        """Der Bug-Fall: Target hat den Alias mit anderem Inhalt.
        Erwartung: setItem/{uuid}, NICHT addItem (das wuerde als
        \"already exists\" fehlschlagen)."""
        captured: dict[str, object] = {}

        def search(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "rows": [
                        {"uuid": "u-existing", "name": "ips"},
                    ]
                },
            )

        def setitem(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = _json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"result": "saved"})

        api = (
            MockApi().on("POST", ALIAS_SEARCH, search).on("POST", ALIAS_SET.split("{")[0], setitem)
        )
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        outcome = adapter.add(
            client,
            ctx,
            AliasSpec(
                name="ips", type="host", content=("10.0.0.1", "10.0.0.2"), merge_mode="replace"
            ),
        )
        assert outcome.uuid == "u-existing"
        assert "u-existing" in str(captured["path"])
        # setItem-Body enthaelt den kompletten Master-Content (nicht Merge)
        sent = captured["body"]
        assert isinstance(sent, dict)
        alias = sent["alias"]
        assert isinstance(alias, dict)
        assert alias["content"] == "10.0.0.1\n10.0.0.2"

    def test_replace_never_appends(self) -> None:
        """Regressions-Sicherung: replace darf NICHT wie append den
        bestehenden Inhalt lesen und mergen. Der Master-Zustand
        ueberschreibt komplett."""
        setitem_bodies: list[dict[str, Any]] = []

        def search(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "rows": [
                        {"uuid": "u1", "name": "ips"},
                    ]
                },
            )

        def setitem(request: httpx.Request) -> httpx.Response:
            setitem_bodies.append(
                _json.loads(request.content.decode("utf-8")),
            )
            return httpx.Response(200, json={"result": "saved"})

        api = (
            MockApi().on("POST", ALIAS_SEARCH, search).on("POST", ALIAS_SET.split("{")[0], setitem)
        )
        client, ctx = _build_client(api)
        adapter = AliasAdapter()
        adapter.add(
            client,
            ctx,
            AliasSpec(name="ips", type="host", content=("only-master",), merge_mode="replace"),
        )
        # getItem darf NICHT gerufen werden bei replace (im Gegensatz zu
        # append). Wir sehen es indirekt: nur die eine Anfrage im setItem
        # und der Content ist exakt was der Master hatte.
        assert len(setitem_bodies) == 1
        assert setitem_bodies[0]["alias"]["content"] == "only-master"


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
            name="x",
            type="host",
            content=("a", "b"),
            merge_mode="append",
        )
        d = adapter.diff(current, target_spec)
        assert d.kind is DiffKind.SKIP

    def test_update_when_append_adds_new(self) -> None:
        adapter = AliasAdapter()
        current = AliasSpec(name="x", type="host", content=("a",))
        target_spec = AliasSpec(
            name="x",
            type="host",
            content=("a", "b", "c"),
            merge_mode="append",
        )
        d = adapter.diff(current, target_spec)
        assert d.kind is DiffKind.UPDATE
        assert "+2" in d.summary

    # --- replace mode (Sync/Upsert) ---

    def test_new_when_replace_and_missing(self) -> None:
        # Sync-Fall: Target hat den Alias nicht -> NEW (Adapter macht add).
        adapter = AliasAdapter()
        d = adapter.diff(
            None,
            AliasSpec(name="x", type="host", content=("a",), merge_mode="replace"),
        )
        assert d.kind is DiffKind.NEW

    def test_skip_when_replace_identical(self) -> None:
        adapter = AliasAdapter()
        current = AliasSpec(name="x", type="host", content=("a", "b"))
        target_spec = AliasSpec(
            name="x",
            type="host",
            content=("b", "a"),
            merge_mode="replace",
        )
        d = adapter.diff(current, target_spec)
        assert d.kind is DiffKind.SKIP

    def test_update_when_replace_differs_no_fail_warning(self) -> None:
        # Der Bug-Fall: create wuerde "Apply wird fehlschlagen" ausgeben,
        # replace muss einen sauberen UPDATE-Diff produzieren.
        adapter = AliasAdapter()
        current = AliasSpec(name="x", type="host", content=("a", "b"))
        target_spec = AliasSpec(
            name="x",
            type="host",
            content=("b", "c", "d"),
            merge_mode="replace",
        )
        d = adapter.diff(current, target_spec)
        assert d.kind is DiffKind.UPDATE
        # Kein "fehlschlagen"-Text mehr, stattdessen konkreter Diff
        assert "fehlschlagen" not in d.summary
        assert "Master-Zustand" in d.summary
        # Content-Delta ist in der Summary sichtbar
        assert "+2" in d.summary  # c und d neu
        assert "-1" in d.summary  # a weg


# ---------------------------------------------------------------------------
# AliasesController.reconfigure
# ---------------------------------------------------------------------------


class TestAliasesController:
    def test_calls_reconfigure(self) -> None:
        api = MockApi().on(
            "POST",
            ALIAS_RECONFIGURE,
            lambda _r: httpx.Response(200, json={"status": "ok"}),
        )
        client, ctx = _build_client(api)
        AliasesController().reconfigure(client, ctx)
        assert any(r.url.path == ALIAS_RECONFIGURE for r in api.received)

    def test_wraps_auth_error(self) -> None:
        api = MockApi().on(
            "POST",
            ALIAS_RECONFIGURE,
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
