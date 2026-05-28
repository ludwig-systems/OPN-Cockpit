"""Generische Verträge für Objekttyp-Adapter und Subsystem-Controller.

Trennung gemäß Plan-Review:

* **`ObjectAdapter`** kennt EINEN Objekttyp (Route, Alias, später Unbound-Host,
  Firewall-Regel). Liefert das, was Planner und Executor pro Objekt brauchen:
  Pre-Check (``exists``), Schreiben (``add``), Diff für die Vorschau,
  Read-back-Verifikation und das maskierungsbereite Payload-Dict.

* **`SubsystemController`** kennt EIN Subsystem (Routen-Subsystem,
  Firewall-Aliasse-Subsystem). Trägt die ``reconfigure``-Operation, die
  pro Gerät **einmal** nach allen Schreibvorgängen ausgeführt wird (R-RUN-1).

Diese Trennung ist die Hebelstelle, an der später Unbound-DNS-Host-Overrides
und Firewall-Regeln als zusätzliche Adapter/Controller andocken können, ohne
dass Orchestrierung oder GUI angepasst werden müssen (Spec §8).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from opn_cockpit.core.http_client import HttpClient, HttpTarget

# ---------------------------------------------------------------------------
# Request-Kontext
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Was ein Adapter pro Aufruf an den ``HttpClient`` durchreichen muss.

    Bündelt Ziel + Credentials in EINEM Objekt, damit Adapter-Signaturen kurz
    bleiben. Wird vom Executor pro Gerät genau einmal aufgebaut, an die
    Adapter durchgereicht und nach Abschluss verworfen (Spec R-SEC-5 verlangt,
    dass das Klartext-Secret nirgendwo persistent gehalten wird).
    """

    target: HttpTarget
    key: str
    secret: str


# ---------------------------------------------------------------------------
# Diff für die Vorschau
# ---------------------------------------------------------------------------


class DiffKind(StrEnum):
    """Klassifikation eines vorbereiteten Schreibvorgangs.

    Wird vom Planner pro Gerät berechnet und in der Preview-Anzeige sichtbar
    gemacht (R-PRE-3). Steuert auch das Verhalten des Executors (z. B. SKIP
    bedeutet: kein ``add``-Call ausführen, R-RUN-5).
    """

    NEW = "new"
    SKIP = "skip"
    UPDATE = "update"


@dataclass(frozen=True, slots=True)
class Diff:
    kind: DiffKind
    summary: str


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


TSpec = TypeVar("TSpec")
TIdent = TypeVar("TIdent")


@runtime_checkable
class ObjectAdapter(Protocol[TSpec, TIdent]):
    """Vertrag eines Adapters für genau einen Objekttyp.

    Implementierungen müssen reine API-Logik enthalten — keine GUI-, keyring-
    oder Logging-Importe. Sie werden gegen ``httpx.MockTransport`` testbar
    (R-NFR-4).
    """

    subsystem: ClassVar[str]

    def identity(self, spec: TSpec) -> TIdent:
        """Extrahiert die Identitätsschlüssel aus einer kompletten Spec."""

    def exists(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: TIdent,
    ) -> TSpec | None:
        """Lädt den aktuellen Zustand des Objekts oder ``None``, wenn keiner existiert.

        Wird vom Planner für den Pre-Check verwendet (Diff-Berechnung).
        """

    def add(
        self,
        client: HttpClient,
        ctx: RequestContext,
        spec: TSpec,
    ) -> AddOutcome:
        """Schreibt das Objekt in die Konfiguration des Geräts.

        Aktiviert es noch NICHT — dafür ist ``SubsystemController.reconfigure``
        zuständig. Aufrufer dürfen ``add`` mehrfach für unterschiedliche Specs
        aufrufen, bevor sie einmal ``reconfigure`` triggern.
        """

    def verify(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: TIdent,
    ) -> VerifyOutcome:
        """Liest das Objekt nach ``reconfigure`` aus und meldet ``found``-Status.

        Nur ein erfolgreiches Read-back zählt als bestandene Verifikation
        (R-RUN-2). Die ``add``-Antwort ist ausdrücklich kein Verifikationsbeleg.
        """

    def diff(self, current: TSpec | None, target_spec: TSpec) -> Diff:
        """Vergleicht aktuellen Zustand mit dem Soll-Zustand.

        Liefert ``DiffKind.NEW`` / ``SKIP`` / ``UPDATE`` und eine kurze,
        menschlich lesbare Zusammenfassung für die Vorschau.
        """

    def to_payload(self, spec: TSpec) -> dict[str, Any]:
        """Erzeugt das API-Payload-Dict.

        Wird vom Planner aufgerufen, um den maskierten Payload für die
        Vorschau zu erzeugen, und vom Adapter intern beim ``add``-Call wieder
        verwendet. Enthält keine Secrets.
        """

    def spec_to_dict(self, spec: TSpec) -> dict[str, Any]:
        """Serialisiert eine Spec für die persistente Plan-Datei.

        Wird vom ``PlanStore`` aufgerufen, wenn ein Plan auf Platte landet
        (zwischen ``plan`` und ``apply`` per CLI). Roundtrip-fähig mit
        :meth:`spec_from_dict`.
        """

    def spec_from_dict(self, raw: dict[str, Any]) -> TSpec:
        """Rekonstruiert eine Spec aus dem Plan-File.

        Defensiv: unbekannte Felder werden ignoriert, fehlende mit Defaults
        gefüllt — überlebt v1→v2-Schema-Erweiterungen.
        """


@runtime_checkable
class SubsystemController(Protocol):
    """Trägt die ``reconfigure``-Operation eines Subsystems.

    Wird vom Executor **einmal** pro Gerät pro Subsystem aufgerufen, nachdem
    alle ``add``-Calls dieses Subsystems durch sind. Wirft
    ``ReconfigureError`` bei Fehler — der Executor verbucht das Gerät dann
    als ``Status.WRITTEN`` mit ``failed_phase=ACTIVATE``.
    """

    subsystem: ClassVar[str]

    def reconfigure(self, client: HttpClient, ctx: RequestContext) -> None: ...


# ---------------------------------------------------------------------------
# Re-Export der Outcome-Typen aus core.result
# ---------------------------------------------------------------------------

# Adapter und Controller liefern ``AddOutcome`` / ``VerifyOutcome`` zurück.
# Diese leben in ``core.result``, damit die Orchestrierung sie direkt in
# ``Result`` einbetten kann. Wir re-exportieren sie hier, damit Adapter-
# Implementierer nicht auf zwei Module zugreifen müssen.

from opn_cockpit.core.result import AddOutcome, VerifyOutcome  # noqa: E402

__all__ = [
    "AddOutcome",
    "Diff",
    "DiffKind",
    "ObjectAdapter",
    "RequestContext",
    "SubsystemController",
    "VerifyOutcome",
]
