"""Tests fuer core.objects.unbound - Domain-Overrides + Query-Forwards.

Der Host-Adapter selbst wird durch die Live-Tests gegen die Test-OPNsense
abgedeckt; hier ist der Fokus auf den zwei neuen Adaptern (Domain +
Forward) mit ihrer Payload-, Diff- und CRUD-Logik.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning
from opn_cockpit.core.objects._endpoints import (
    UNBOUND_DOMAIN_ADD,
    UNBOUND_DOMAIN_GET,
    UNBOUND_DOMAIN_SEARCH,
    UNBOUND_DOMAIN_SET,
    UNBOUND_FORWARD_ADD,
    UNBOUND_FORWARD_DEL,
    UNBOUND_FORWARD_SEARCH,
    UNBOUND_RECONFIGURE,
)
from opn_cockpit.core.objects.base import DiffKind, RequestContext
from opn_cockpit.core.objects.unbound import (
    UnboundDomainAdapter,
    UnboundDomainIdentity,
    UnboundDomainsController,
    UnboundDomainSpec,
    UnboundForwardAdapter,
    UnboundForwardIdentity,
    UnboundForwardsController,
    UnboundForwardSpec,
)

# ---------------------------------------------------------------------------
# Test-Helfer (identisch zu test_objects_routes.MockApi)
# ---------------------------------------------------------------------------


@dataclass
class _Route:
    path: str
    handler: Callable[[httpx.Request], httpx.Response]


@dataclass
class MockApi:
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
        return httpx.Response(
            404,
            json={"error": "no route: " + request.url.path},
        )


def _build_client(api: MockApi) -> tuple[HttpClient, RequestContext]:
    transport = httpx.MockTransport(api)
    target = HttpTarget(host="opn-a.lab", port=443, verify=False)
    client = HttpClient(
        targets=[target],
        tuning=HttpTuning(retry_count=0, retry_backoff_base_s=0.0),
        transport=transport,
        sleep=lambda _delay: None,
    )
    ctx = RequestContext(target=target, key="key", secret="topsecret")
    return client, ctx


# ===========================================================================
# Domain-Override-Adapter
# ===========================================================================


class TestDomainSpec:
    def test_spec_to_identity(self) -> None:
        spec = UnboundDomainSpec(domain="lab.local", server="10.0.0.53")
        assert spec.to_identity() == UnboundDomainIdentity(domain="lab.local")

    def test_spec_from_dict_defaults(self) -> None:
        adapter = UnboundDomainAdapter()
        spec = adapter.spec_from_dict({"domain": "x.example"})
        assert spec.domain == "x.example"
        assert spec.server == ""
        assert spec.description == ""
        assert spec.enabled is True

    def test_spec_round_trip_via_dict(self) -> None:
        adapter = UnboundDomainAdapter()
        original = UnboundDomainSpec(
            domain="internal.example.com",
            server="10.0.0.53",
            description="AD-DNS",
            enabled=False,
        )
        clone = adapter.spec_from_dict(adapter.spec_to_dict(original))
        assert clone == original


class TestDomainPayload:
    def test_payload_shape(self) -> None:
        adapter = UnboundDomainAdapter()
        spec = UnboundDomainSpec(
            domain="lab.local",
            server="10.0.0.53",
            description="AD",
            enabled=True,
        )
        payload = adapter.to_payload(spec)
        assert payload == {
            "enabled": "1",
            "domain": "lab.local",
            "server": "10.0.0.53",
            "description": "AD",
        }

    def test_payload_disabled_flag(self) -> None:
        adapter = UnboundDomainAdapter()
        payload = adapter.to_payload(
            UnboundDomainSpec(
                domain="lab.local",
                server="10.0.0.53",
                enabled=False,
            )
        )
        assert payload["enabled"] == "0"


class TestDomainDiff:
    def _adapter(self) -> UnboundDomainAdapter:
        return UnboundDomainAdapter()

    def test_new_when_current_missing(self) -> None:
        target = UnboundDomainSpec(domain="lab.local", server="10.0.0.53")
        assert self._adapter().diff(None, target).kind is DiffKind.NEW

    def test_skip_when_identical(self) -> None:
        target = UnboundDomainSpec(
            domain="lab.local",
            server="10.0.0.53",
            description="X",
            enabled=True,
        )
        assert self._adapter().diff(target, target).kind is DiffKind.SKIP

    def test_update_signals_conflict(self) -> None:
        current = UnboundDomainSpec(domain="lab.local", server="10.0.0.53")
        target = UnboundDomainSpec(domain="lab.local", server="10.0.0.99")
        assert self._adapter().diff(current, target).kind is DiffKind.UPDATE

    def test_update_diff_lists_changes(self) -> None:
        current = UnboundDomainSpec(
            domain="lab.local",
            server="10.0.0.53",
            description="old",
        )
        target = UnboundDomainSpec(
            domain="lab.local",
            server="10.0.0.99",
            description="new",
            enabled=False,
        )
        d = self._adapter().diff_for_update(current, target)
        assert d.kind is DiffKind.UPDATE
        assert "10.0.0.53" in d.summary
        assert "10.0.0.99" in d.summary
        assert "deaktivieren" in d.summary
        assert "Beschreibung" in d.summary

    def test_delete_skip_when_absent(self) -> None:
        d = self._adapter().diff_for_delete(
            None,
            UnboundDomainIdentity(domain="x"),
        )
        assert d.kind is DiffKind.SKIP


# --- CRUD ------------------------------------------------------------------


def _mk_domain_row(
    uuid: str, domain: str, server: str, description: str = "", enabled: str = "1"
) -> dict:
    return {
        "uuid": uuid,
        "domain": domain,
        "server": server,
        "description": description,
        "enabled": enabled,
    }


class TestDomainCrud:
    def test_exists_returns_none_when_missing(self) -> None:
        api = MockApi().on(
            UNBOUND_DOMAIN_SEARCH,
            lambda _req: httpx.Response(200, json={"rows": []}),
        )
        client, ctx = _build_client(api)
        adapter = UnboundDomainAdapter()
        assert adapter.exists(client, ctx, UnboundDomainIdentity("x.example")) is None

    def test_exists_finds_by_domain(self) -> None:
        row = _mk_domain_row("u1", "lab.local", "10.0.0.53")
        api = (
            MockApi()
            .on(UNBOUND_DOMAIN_SEARCH, lambda _req: httpx.Response(200, json={"rows": [row]}))
            .on(
                UNBOUND_DOMAIN_GET.format(uuid="u1"),
                lambda _req: httpx.Response(200, json={"domain": row}),
            )
        )
        client, ctx = _build_client(api)
        adapter = UnboundDomainAdapter()
        current = adapter.exists(
            client,
            ctx,
            UnboundDomainIdentity("lab.local"),
        )
        assert current is not None
        assert current.domain == "lab.local"
        assert current.server == "10.0.0.53"

    def test_add_wraps_payload_under_domain_key(self) -> None:
        captured: dict = {}

        def _add(req: httpx.Request) -> httpx.Response:
            captured["json"] = _json_body(req)
            return httpx.Response(200, json={"uuid": "new-uuid", "result": "saved"})

        api = MockApi().on(UNBOUND_DOMAIN_ADD, _add)
        client, ctx = _build_client(api)
        outcome = UnboundDomainAdapter().add(
            client,
            ctx,
            UnboundDomainSpec(domain="lab.local", server="10.0.0.53"),
        )
        assert outcome.uuid == "new-uuid"
        assert "domain" in captured["json"]
        assert captured["json"]["domain"]["domain"] == "lab.local"

    def test_update_needs_search_then_set(self) -> None:
        row = _mk_domain_row("existing-uuid", "lab.local", "10.0.0.53")
        set_path = UNBOUND_DOMAIN_SET.format(uuid="existing-uuid")
        api = (
            MockApi()
            .on(UNBOUND_DOMAIN_SEARCH, lambda _req: httpx.Response(200, json={"rows": [row]}))
            .on(set_path, lambda _req: httpx.Response(200, json={"result": "saved"}))
        )
        client, ctx = _build_client(api)
        outcome = UnboundDomainAdapter().update(
            client,
            ctx,
            UnboundDomainSpec(domain="lab.local", server="10.99.99.99"),
        )
        assert outcome.uuid == "existing-uuid"

    def test_delete_no_op_when_missing(self) -> None:
        api = MockApi().on(
            UNBOUND_DOMAIN_SEARCH,
            lambda _req: httpx.Response(200, json={"rows": []}),
        )
        client, ctx = _build_client(api)
        outcome = UnboundDomainAdapter().delete(
            client,
            ctx,
            UnboundDomainIdentity("gone.example"),
        )
        assert outcome.uuid is None


class TestDomainsController:
    def test_reconfigure_posts_correct_path(self) -> None:
        api = MockApi().on(
            UNBOUND_RECONFIGURE,
            lambda _req: httpx.Response(200, json={"status": "ok"}),
        )
        client, ctx = _build_client(api)
        UnboundDomainsController().reconfigure(client, ctx)
        paths = [r.url.path for r in api.received]
        assert paths == [UNBOUND_RECONFIGURE]

    def test_reconfigure_wraps_error(self) -> None:
        api = MockApi().on(
            UNBOUND_RECONFIGURE,
            lambda _req: httpx.Response(500, json={"error": "boom"}),
        )
        client, ctx = _build_client(api)
        import pytest

        from opn_cockpit.core.errors import ReconfigureError

        with pytest.raises(ReconfigureError):
            UnboundDomainsController().reconfigure(client, ctx)


# ===========================================================================
# Query-Forward-Adapter
# ===========================================================================


class TestForwardSpec:
    def test_default_identity_uses_domain_server_port(self) -> None:
        spec = UnboundForwardSpec(domain="lab.local", server="1.1.1.1", port=53)
        assert spec.to_identity() == UnboundForwardIdentity(
            domain="lab.local",
            server="1.1.1.1",
            port=53,
        )

    def test_identity_treats_empty_domain_as_global(self) -> None:
        spec = UnboundForwardSpec(server="1.1.1.1", port=53)
        assert spec.to_identity().domain == ""

    def test_spec_round_trip_via_dict(self) -> None:
        adapter = UnboundForwardAdapter()
        original = UnboundForwardSpec(
            domain="",
            server="1.1.1.1",
            port=853,
            type="dot",
            verify="cloudflare-dns.com",
            description="Cloudflare DoT",
            enabled=True,
        )
        clone = adapter.spec_from_dict(adapter.spec_to_dict(original))
        assert clone == original


class TestForwardPayload:
    def test_payload_serialises_port_as_string(self) -> None:
        adapter = UnboundForwardAdapter()
        payload = adapter.to_payload(
            UnboundForwardSpec(
                server="1.1.1.1",
                port=853,
                type="dot",
                verify="cloudflare-dns.com",
            )
        )
        assert payload["port"] == "853"
        assert payload["type"] == "dot"
        assert payload["verify"] == "cloudflare-dns.com"

    def test_payload_defaults_type_when_empty(self) -> None:
        adapter = UnboundForwardAdapter()
        # spec.type explicit "" - to_payload should backfill "forward"
        spec = UnboundForwardSpec(server="1.1.1.1", type="")
        payload = adapter.to_payload(spec)
        assert payload["type"] == "forward"


class TestForwardDiff:
    def _adapter(self) -> UnboundForwardAdapter:
        return UnboundForwardAdapter()

    def test_new_when_missing(self) -> None:
        target = UnboundForwardSpec(server="1.1.1.1")
        assert self._adapter().diff(None, target).kind is DiffKind.NEW

    def test_skip_when_identical(self) -> None:
        target = UnboundForwardSpec(
            domain="",
            server="1.1.1.1",
            port=53,
            type="forward",
            verify="",
            description="",
            enabled=True,
        )
        assert self._adapter().diff(target, target).kind is DiffKind.SKIP

    def test_update_diff_reflects_type_change(self) -> None:
        current = UnboundForwardSpec(
            domain="",
            server="1.1.1.1",
            port=53,
            type="forward",
        )
        target = UnboundForwardSpec(
            domain="",
            server="1.1.1.1",
            port=53,
            type="dot",
            verify="cloudflare-dns.com",
        )
        d = self._adapter().diff_for_update(current, target)
        assert d.kind is DiffKind.UPDATE
        assert "Typ" in d.summary
        assert "Verify" in d.summary


def _json_body(req: httpx.Request) -> dict:
    import json

    return json.loads(req.content.decode("utf-8"))


class TestForwardCrud:
    def test_search_uuid_matches_by_triple(self) -> None:
        rows = [
            {
                "uuid": "a",
                "domain": "",
                "server": "1.1.1.1",
                "port": "53",
                "type": "forward",
                "enabled": "1",
            },
            {
                "uuid": "b",
                "domain": "",
                "server": "1.1.1.1",
                "port": "853",
                "type": "dot",
                "verify": "cloudflare-dns.com",
                "enabled": "1",
            },
        ]
        api = MockApi().on(
            UNBOUND_FORWARD_SEARCH,
            lambda _req: httpx.Response(200, json={"rows": rows}),
        )
        client, ctx = _build_client(api)
        adapter = UnboundForwardAdapter()
        # Trifft nur "b" wegen Port 853
        uuid = adapter._search_uuid(
            client,
            ctx,
            UnboundForwardIdentity(domain="", server="1.1.1.1", port=853),
        )
        assert uuid == "b"

    def test_add_wraps_payload_under_forward_key(self) -> None:
        captured: dict = {}

        def _add(req: httpx.Request) -> httpx.Response:
            captured["json"] = _json_body(req)
            return httpx.Response(200, json={"uuid": "new-fwd", "result": "saved"})

        api = MockApi().on(UNBOUND_FORWARD_ADD, _add)
        client, ctx = _build_client(api)
        outcome = UnboundForwardAdapter().add(
            client,
            ctx,
            UnboundForwardSpec(server="1.1.1.1"),
        )
        assert outcome.uuid == "new-fwd"
        assert "forward" in captured["json"]
        assert captured["json"]["forward"]["server"] == "1.1.1.1"

    def test_delete_is_noop_when_missing(self) -> None:
        api = MockApi().on(
            UNBOUND_FORWARD_SEARCH,
            lambda _req: httpx.Response(200, json={"rows": []}),
        )
        client, ctx = _build_client(api)
        outcome = UnboundForwardAdapter().delete(
            client,
            ctx,
            UnboundForwardIdentity(domain="", server="9.9.9.9", port=53),
        )
        assert outcome.uuid is None

    def test_delete_hits_del_endpoint_when_present(self) -> None:
        rows = [
            {
                "uuid": "kill-me",
                "domain": "",
                "server": "1.1.1.1",
                "port": "53",
                "type": "forward",
                "enabled": "1",
            }
        ]
        del_path = UNBOUND_FORWARD_DEL.format(uuid="kill-me")
        api = (
            MockApi()
            .on(UNBOUND_FORWARD_SEARCH, lambda _req: httpx.Response(200, json={"rows": rows}))
            .on(del_path, lambda _req: httpx.Response(200, json={"result": "deleted"}))
        )
        client, ctx = _build_client(api)
        outcome = UnboundForwardAdapter().delete(
            client,
            ctx,
            UnboundForwardIdentity(domain="", server="1.1.1.1", port=53),
        )
        assert outcome.uuid == "kill-me"
        assert del_path in [r.url.path for r in api.received]


class TestForwardsController:
    def test_reconfigure_shares_unbound_endpoint(self) -> None:
        api = MockApi().on(
            UNBOUND_RECONFIGURE,
            lambda _req: httpx.Response(200, json={"status": "ok"}),
        )
        client, ctx = _build_client(api)
        UnboundForwardsController().reconfigure(client, ctx)
        assert [r.url.path for r in api.received] == [UNBOUND_RECONFIGURE]


# ===========================================================================
# Registry-Integration
# ===========================================================================


class TestRegistryBindings:
    def test_domain_binding_matches_adapter_subsystem(self) -> None:
        from opn_cockpit.orchestration.registry import get_binding

        binding = get_binding("unbound_domains")
        assert isinstance(binding.adapter, UnboundDomainAdapter)
        assert binding.adapter.subsystem == "unbound_domains"
        assert binding.controller.subsystem == "unbound_domains"

    def test_forward_binding_matches_adapter_subsystem(self) -> None:
        from opn_cockpit.orchestration.registry import get_binding

        binding = get_binding("unbound_forwards")
        assert isinstance(binding.adapter, UnboundForwardAdapter)
        assert binding.adapter.subsystem == "unbound_forwards"
        assert binding.controller.subsystem == "unbound_forwards"
