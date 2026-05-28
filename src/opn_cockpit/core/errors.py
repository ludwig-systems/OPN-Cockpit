"""Fehlertypen des Core-Layers.

Verbindlich (siehe Plan, R-NFR-3 + R-SEC-5):

* Klare, unterscheidbare Ursachen — kein einziger Allzweck-Typ.
* **Niemals** rohe HTTP-Bodies in einer Exception transportieren. Die
  Exception trägt nur strukturierte Felder (``status_code``, ``error_kind``)
  und eine deliberate kurze, vorab gefilterte Zusammenfassung.
* Höhere Schichten (Adapter, Orchestrierung) wrappen low-level Fehler in
  semantisch passende Typen (``ReconfigureError`` / ``VerificationError``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ErrorContext:
    """Strukturierte Begleitinformation einer Exception.

    Felder sind alle **kurz** und **frei von Klartext-Secrets**. Wer einen
    Wert hier reinschreibt, gibt damit ab, dass dieser Wert in Audit-Log
    und UI sichtbar sein darf.
    """

    host: str | None = None
    port: int | None = None
    method: str | None = None
    path: str | None = None
    status_code: int | None = None
    error_kind: str | None = None
    summary: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "method": self.method,
            "path": self.path,
            "status_code": self.status_code,
            "error_kind": self.error_kind,
            "summary": self.summary,
        }


class OpnCockpitError(Exception):
    """Basisklasse aller Tool-eigenen Fehler."""

    default_kind: str = "unknown"

    def __init__(self, message: str, *, context: ErrorContext | None = None) -> None:
        super().__init__(message)
        self.context: ErrorContext = context or ErrorContext(error_kind=self.default_kind)


class EgressDeniedError(OpnCockpitError):
    """Ein HTTP-Request wurde gegen einen Host gestellt, der nicht im Inventar steht.

    Adressiert R-SEC-7: die App spricht ausschließlich mit konfigurierten
    OPNsense-Hosts. Verhindert versehentliche Drittanbieter-Calls.
    """

    default_kind = "egress_denied"


class UnreachableError(OpnCockpitError):
    """Netzwerk-Ebene fehlgeschlagen.

    Umfasst Connection refused, DNS-Fehler, TLS-Handshake-Fehler, Timeouts.
    Wird nach erschöpften Retries geworfen.
    """

    default_kind = "unreachable"


class AuthError(OpnCockpitError):
    """API-Key oder Secret abgelehnt (HTTP 401/403)."""

    default_kind = "auth"


class ValidationError(OpnCockpitError):
    """Eingabe wurde clientseitig oder von der OPNsense-API abgelehnt.

    Umfasst CIDR-Parse-Fehler, ungültige Alias-Namen, 4xx-Antworten der API
    (außer 401/403, die auf ``AuthError`` gehen).
    """

    default_kind = "validation"


class ApiError(OpnCockpitError):
    """Server-seitiger Fehler (5xx) nach erschöpften Retries.

    Wird von Adaptern oft kontextspezifisch weiter-gewrappt (z. B. zu
    ``ReconfigureError``), damit die Orchestrierung erkennen kann, in
    welcher Phase es brach.
    """

    default_kind = "api"


class ReconfigureError(OpnCockpitError):
    """Der ``reconfigure``-Aufruf für ein Subsystem auf einem Gerät schlug fehl.

    Bedeutet konkret: Schreibvorgänge sind erfolgt, aber die Aktivierung
    blieb aus. Das Gerät befindet sich in einem inkonsistenten Zustand,
    den der Administrator manuell prüfen muss.
    """

    default_kind = "reconfigure"


class VerificationError(OpnCockpitError):
    """Read-back konnte den eben geschriebenen Eintrag nicht bestätigen (R-RUN-2).

    Tritt nach erfolgreichem ``add`` + ``reconfigure`` auf, wenn das
    Such-/Get-Endpoint den Eintrag nicht zurückgibt. Im Plan als härtester
    Hinweis auf eine reale Inkonsistenz markiert.
    """

    default_kind = "verification"


def make_context(
    *,
    host: str | None = None,
    port: int | None = None,
    method: str | None = None,
    path: str | None = None,
    status_code: int | None = None,
    error_kind: str | None = None,
    summary: str = "",
    summary_max_len: int = 200,
) -> ErrorContext:
    """Bequemer Konstruktor.

    Schneidet ``summary`` defensiv auf eine maximale Länge ab, damit selbst
    bei einer Fehlbedienung kein größerer Response-Body in die Exception
    rutschen kann.
    """
    truncated = summary if len(summary) <= summary_max_len else summary[: summary_max_len - 1] + "…"
    return ErrorContext(
        host=host,
        port=port,
        method=method,
        path=path,
        status_code=status_code,
        error_kind=error_kind,
        summary=truncated,
    )
