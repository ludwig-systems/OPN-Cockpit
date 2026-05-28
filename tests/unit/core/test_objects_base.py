"""Tests für core.objects.base — Protocol-Vertrag und Diff-Datentyp."""

from __future__ import annotations

import dataclasses
from typing import ClassVar

import pytest

from opn_cockpit.core.http_client import HttpTarget
from opn_cockpit.core.objects.base import (
    AddOutcome,
    Diff,
    DiffKind,
    ObjectAdapter,
    RequestContext,
    SubsystemController,
    VerifyOutcome,
)
from opn_cockpit.core.objects.routes import RouteAdapter, RoutesController


class TestRequestContext:
    def test_holds_target_and_credentials(self) -> None:
        target = HttpTarget(host="opn-a.lab", port=443, verify=False)
        ctx = RequestContext(target=target, key="k", secret="s")
        assert ctx.target is target
        assert ctx.key == "k"
        assert ctx.secret == "s"

    def test_is_frozen(self) -> None:
        target = HttpTarget(host="h", verify=False)
        ctx = RequestContext(target=target, key="k", secret="s")
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.key = "x"  # type: ignore[misc]


class TestDiffKind:
    def test_values(self) -> None:
        assert DiffKind.NEW == "new"
        assert DiffKind.SKIP == "skip"
        assert DiffKind.UPDATE == "update"


class TestProtocolConformance:
    """Vertragstest: RouteAdapter/RoutesController erfüllen die Protocols.

    Wenn jemand eine Methode aus dem Protocol entfernt oder umbenennt, soll
    dieser Test fehlschlagen, bevor der Executor sich darüber wundert.
    """

    def test_route_adapter_implements_object_adapter(self) -> None:
        adapter = RouteAdapter()
        assert isinstance(adapter, ObjectAdapter)
        assert adapter.subsystem == "routes"

    def test_routes_controller_implements_subsystem_controller(self) -> None:
        controller = RoutesController()
        assert isinstance(controller, SubsystemController)
        assert controller.subsystem == "routes"


class TestCustomAdapterCanImplementProtocol:
    """Sicherstellt, dass spätere Adapter (Alias, Unbound, Firewall) anhängen können.

    Ein winziger Stub-Adapter wird gegen das Protocol geprüft — wenn das
    Protocol später um Methoden ergänzt wird, fällt dieser Test auf, weil
    der Stub die neue Methode nicht implementiert.
    """

    def test_minimal_implementation_satisfies_protocol(self) -> None:
        class _Spec:
            pass

        class _Ident:
            pass

        class _StubAdapter:
            subsystem: ClassVar[str] = "stub"

            def identity(self, spec):  # type: ignore[no-untyped-def]
                return _Ident()

            def exists(self, client, ctx, ident):  # type: ignore[no-untyped-def]
                return None

            def add(self, client, ctx, spec):  # type: ignore[no-untyped-def]
                return AddOutcome(uuid="x", raw_status=200)

            def verify(self, client, ctx, ident):  # type: ignore[no-untyped-def]
                return VerifyOutcome(found=True)

            def diff(self, current, target_spec):  # type: ignore[no-untyped-def]
                return Diff(kind=DiffKind.NEW, summary="x")

            def to_payload(self, spec):  # type: ignore[no-untyped-def]
                return {}

            def spec_to_dict(self, spec):  # type: ignore[no-untyped-def]
                return {}

            def spec_from_dict(self, raw):  # type: ignore[no-untyped-def]
                return _Spec()

        assert isinstance(_StubAdapter(), ObjectAdapter)


class TestDiff:
    def test_diff_carries_kind_and_summary(self) -> None:
        diff = Diff(kind=DiffKind.NEW, summary="Hinzufügen von Route X")
        assert diff.kind is DiffKind.NEW
        assert "Route X" in diff.summary
