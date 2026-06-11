"""Ergebnisrepräsentation für eine Aktion auf einem Gerät.

Folgt R-RUN-3 (Statusdarstellung je Gerät: Geschrieben / Aktiviert (reconfigure
ok) / Verifiziert / Fehlgeschlagen) und ergänzt um "Übersprungen" (R-RUN-5,
Idempotenz).

**Vertrag:** ``Result``- und ``AddOutcome``-Instanzen halten **keine** rohen
HTTP-Bodies. Alles, was hier landet, ist als geeignet für Audit-Log und UI
gekennzeichnet (kurze Strings, strukturierte Felder, keine Secrets).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Phase(StrEnum):
    """Phasen der Rollout-Pipeline pro Gerät pro Subsystem.

    Reihenfolge entspricht der Ausführung im Executor (Plan-Schritt 6):
    erst Pre-Check, dann Write, dann genau einmal Activate, dann Verify.
    """

    PRE_CHECK = "pre-check"
    WRITE = "write"
    ACTIVATE = "activate"
    VERIFY = "verify"


class Status(StrEnum):
    """Höchster erreichter Phasenabschluss eines Geräts in einer Rollout-Aktion.

    Sortiert nach "Fortschritt":

    * ``SKIPPED`` — Pre-Check hat ergeben, dass das Zielobjekt bereits korrekt
      existiert; es wurde nichts geschrieben (Idempotenz, R-RUN-5).
    * ``WRITTEN`` — ``add``-Aufruf war erfolgreich, ``reconfigure`` noch nicht
      ausgeführt oder schlug aus.
    * ``ACTIVATED`` — ``reconfigure`` erfolgreich, Verifikation noch ausstehend
      oder fehlgeschlagen.
    * ``VERIFIED`` — Read-back hat den Eintrag bestätigt. **Erfolgsfall.**
    * ``FAILED`` — In einer Phase abgebrochen; ``failed_phase`` und
      ``error_kind`` halten Details.
    """

    SKIPPED = "Übersprungen"
    WRITTEN = "Geschrieben"
    ACTIVATED = "Aktiviert"
    VERIFIED = "Verifiziert"
    FAILED = "Fehlgeschlagen"


SUCCESS_STATUSES: frozenset[Status] = frozenset({Status.VERIFIED, Status.SKIPPED})
"""Status-Werte, die im aggregierten Bericht als "Erfolg" zählen."""


@dataclass(frozen=True, slots=True)
class AddOutcome:
    """Strukturiertes Ergebnis eines ``add``-Calls auf einem Gerät.

    ``raw_status`` ist der HTTP-Statuscode, ``uuid`` ist die optional von der
    OPNsense-API zurückgegebene Objekt-UUID. Bewusst **kein** ``body``-Feld.
    """

    uuid: str | None = None
    raw_status: int = 0


@dataclass(frozen=True, slots=True)
class VerifyOutcome:
    """Strukturiertes Ergebnis eines Read-back-Aufrufs.

    ``found`` ist die einzige verbindliche Information für R-RUN-2:
    Verifikation gilt nur als bestanden, wenn der Such-/Get-Endpunkt den
    Eintrag tatsächlich zurückgibt. ``detail`` ist optional und enthält
    eine kurze, vorab gefilterte Hilfsinfo (z. B. die UUID des gefundenen
    Eintrags), niemals den vollen Antwort-Body.
    """

    found: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Result:
    """Endgültiges Ergebnis pro Gerät für eine ausgerollte Aktion.

    Eine Instanz repräsentiert das, was im Audit-Log und in der GUI-Result-
    Matrix erscheint. Pflichtfelder reichen für die aggregierte Übersicht.

    ``safety_net_status`` markiert pro Gerät den Zustand des Dead-Man's-
    Switch:

    * ``""`` — Safety-Net nicht aktiv (Default, kein UI-Hinweis).
    * ``"armed"`` — Apply hat den Daemon armed, Disarm liegt noch vor uns.
      Kommt eigentlich nur als Übergangszustand vor.
    * ``"disarmed"`` — Cleanup direkt nach Verify gelungen (Happy Path).
    * ``"disarm_pending"`` — Cleanup hat im Executor versagt; der Watcher
      hat die Aufgabe übernommen und versucht weiter. UI zeigt Banner.
    """

    device_id: str
    subsystem: str
    status: Status
    short_message: str = ""
    error_kind: str | None = None
    failed_phase: Phase | None = None
    duration_ms: int = 0
    add_outcome: AddOutcome | None = None
    verify_outcome: VerifyOutcome | None = None
    safety_net_status: str = ""

    def is_success(self) -> bool:
        return self.status in SUCCESS_STATUSES


@dataclass(frozen=True, slots=True)
class RolloutReport:
    """Aggregierte Statusmatrix einer Rollout-Aktion über alle Zielgeräte.

    Genutzt von der Orchestrierung (Schritt 6) als Rückgabewert und von der
    GUI als Datenquelle der Result-Matrix.
    """

    results: tuple[Result, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def successes(self) -> int:
        return sum(1 for r in self.results if r.is_success())

    @property
    def failures(self) -> int:
        return sum(1 for r in self.results if r.status is Status.FAILED)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status is Status.SKIPPED)

    def all_successful(self) -> bool:
        return self.failures == 0 and self.total > 0
