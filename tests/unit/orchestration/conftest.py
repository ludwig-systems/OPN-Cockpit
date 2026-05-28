"""Fixtures für Orchestrierungs-Tests.

Stellt Hilfen bereit:

* :class:`FakeAdapter` / :class:`FakeController` — minimaler Adapter/Controller,
  der nicht über HTTP arbeitet, sondern in-memory deterministische Antworten
  liefert. Ideal für Executor-Tests, weil das Phasen-Verhalten ohne
  HTTP-Mock-Pyramide getestet werden kann.
* :func:`make_session` — entsperrte ``Session`` mit beliebigen
  ``VaultDevice``-Einträgen.
* :func:`make_audit` — ``AuditLog`` auf einem tmp-Pfad.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from opn_cockpit.audit.log import AuditLog
from opn_cockpit.core.errors import OpnCockpitError
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning
from opn_cockpit.core.objects.base import (
    AddOutcome,
    Diff,
    DiffKind,
    RequestContext,
    VerifyOutcome,
)
from opn_cockpit.security.session import Session
from opn_cockpit.vault.format import VaultHeader
from opn_cockpit.vault.model import VaultData, VaultDevice, VaultSettings
from opn_cockpit.vault.store import OpenedVault

# ---------------------------------------------------------------------------
# FakeAdapter / FakeController
# ---------------------------------------------------------------------------


@dataclass
class FakeAdapter:
    """Minimaler ObjectAdapter ohne HTTP — nur in-memory Mappings.

    Konfigurierbar:
    * ``existing`` — Dict {device_host: spec} für `exists`-Antworten
    * ``add_raises`` — Dict {device_host: Exception} für simulierte WRITE-Fehler
    * ``verify_returns`` — Dict {device_host: VerifyOutcome} für VERIFY-Tests
    """

    subsystem: ClassVar[str] = "test_subsystem"
    existing: dict[str, Any] = field(default_factory=dict)
    add_raises: dict[str, Exception] = field(default_factory=dict)
    verify_returns: dict[str, VerifyOutcome] = field(default_factory=dict)
    add_calls: list[tuple[str, Any]] = field(default_factory=list)
    verify_calls: list[tuple[str, Any]] = field(default_factory=list)

    def identity(self, spec: Any) -> str:
        return f"id::{spec}"

    def exists(self, client: HttpClient, ctx: RequestContext, ident: str) -> Any | None:
        return self.existing.get(ctx.target.host)

    def add(self, client: HttpClient, ctx: RequestContext, spec: Any) -> AddOutcome:
        self.add_calls.append((ctx.target.host, spec))
        if ctx.target.host in self.add_raises:
            raise self.add_raises[ctx.target.host]
        return AddOutcome(uuid=f"uuid-{ctx.target.host}", raw_status=200)

    def verify(
        self, client: HttpClient, ctx: RequestContext, ident: str
    ) -> VerifyOutcome:
        self.verify_calls.append((ctx.target.host, ident))
        return self.verify_returns.get(
            ctx.target.host, VerifyOutcome(found=True, detail="ok")
        )

    def diff(self, current: Any | None, target_spec: Any) -> Diff:
        if current is None:
            return Diff(kind=DiffKind.NEW, summary=f"NEW {target_spec}")
        return Diff(kind=DiffKind.SKIP, summary=f"existiert: {target_spec}")

    def to_payload(self, spec: Any) -> dict[str, Any]:
        return {"spec": str(spec)}

    def spec_to_dict(self, spec: Any) -> dict[str, Any]:
        return {"value": str(spec)}

    def spec_from_dict(self, raw: dict[str, Any]) -> Any:
        return raw.get("value", "")


@dataclass
class FakeController:
    subsystem: ClassVar[str] = "test_subsystem"
    raises_for: dict[str, Exception] = field(default_factory=dict)
    reconfigure_calls: list[str] = field(default_factory=list)

    def reconfigure(self, client: HttpClient, ctx: RequestContext) -> None:
        self.reconfigure_calls.append(ctx.target.host)
        if ctx.target.host in self.raises_for:
            raise self.raises_for[ctx.target.host]


# ---------------------------------------------------------------------------
# Fixture-Helfer
# ---------------------------------------------------------------------------


def make_session(devices: list[VaultDevice], *, inactivity_minutes: int = 10) -> Session:
    data = VaultData(
        devices=devices,
        settings=VaultSettings(inactivity_minutes=inactivity_minutes),
    )
    header = VaultHeader(
        version=1,
        kdf_salt=b"\x00" * 16,
        kdf_time_cost=1,
        kdf_memory_cost_kib=8,
        kdf_parallelism=1,
        nonce=b"\x00" * 12,
    )
    session = Session()
    session.unlock(OpenedVault(data=data, header=header), Path("dummy.opnvault"))
    return session


def make_audit(tmp_path: Path) -> AuditLog:
    return AuditLog(path=tmp_path / "audit.jsonl", actor="test")


def make_client_for_hosts(
    hosts: list[str],
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> HttpClient:
    """HttpClient mit MockTransport für die Hosts.

    Default-Handler liefert immer 200 + leeres JSON.
    """
    transport = httpx.MockTransport(handler or (lambda r: httpx.Response(200, json={})))
    targets = [HttpTarget(host=h, port=443, verify=False) for h in hosts]
    return HttpClient(
        targets=targets,
        tuning=HttpTuning(retry_count=0),
        transport=transport,
        sleep=lambda _delay: None,
    )


# ---------------------------------------------------------------------------
# Fixture-Decorators
# ---------------------------------------------------------------------------


@pytest.fixture()
def audit(tmp_path: Path) -> AuditLog:
    return make_audit(tmp_path)


@pytest.fixture()
def fake_adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture()
def fake_controller() -> FakeController:
    return FakeController()


# Re-export für convenience
__all__ = [
    "FakeAdapter",
    "FakeController",
    "OpnCockpitError",  # für Tests, die Exceptions auswerfen
    "make_audit",
    "make_client_for_hosts",
    "make_session",
]
