"""Verbindungstest pro Gerät — leichtgewichtiger Read-Endpunkt + Auth-Probe.

Erfüllt Spec **R-DEV-3** ("Test connection"). Wird vom CLI-Sub-Command
``test-connection`` und später vom GUI-Inventar-View aufgerufen.

Verwendet einen kostengünstigen GET-Endpunkt der OPNsense, der ohne
Schreibvorgang Erreichbarkeit + gültige API-Credentials bestätigt. Aktuell:
``/api/core/menu/tree`` (Standard-OPNsense-Endpoint, der die Menüstruktur
zurückgibt). Wird mit Schritt 0 (API-Spike) ggf. gegen einen noch
schmaleren Endpunkt getauscht.
"""

from __future__ import annotations

from dataclasses import dataclass

from opn_cockpit.core.errors import AuthError, OpnCockpitError, UnreachableError
from opn_cockpit.core.http_client import HttpClient, HttpTarget

HEALTH_ENDPOINT = "/api/core/menu/tree"


@dataclass(frozen=True, slots=True)
class HealthResult:
    """Ergebnis eines ``check_device``-Aufrufs.

    ``reachable`` und ``authenticated`` lassen sich getrennt auswerten:
    Eine 401-Antwort heißt "Netzwerk OK, aber Schlüssel falsch" —
    deutlich anders als "Host nicht erreichbar".
    """

    reachable: bool
    authenticated: bool
    summary: str

    @property
    def is_ok(self) -> bool:
        return self.reachable and self.authenticated


def check_device(
    client: HttpClient,
    target: HttpTarget,
    key: str,
    secret: str,
) -> HealthResult:
    """Prüft Erreichbarkeit + API-Auth gegen ein einzelnes Gerät.

    Diese Funktion ist bewusst frei von Audit-/Session-/CLI-Konzepten —
    sie ist die kleinste sinnvolle Schicht-1-Operation und lässt sich von
    Orchestrierung, CLI und GUI gleichermaßen aufrufen.
    """
    try:
        client.call(target, key, secret, "GET", HEALTH_ENDPOINT)
        return HealthResult(
            reachable=True,
            authenticated=True,
            summary=f"erreichbar + authentifiziert ({target.host})",
        )
    except AuthError as exc:
        reason = exc.context.summary or "Schlüssel/Secret falsch"
        return HealthResult(
            reachable=True,
            authenticated=False,
            summary=f"erreichbar, aber Auth abgelehnt: {reason}",
        )
    except UnreachableError as exc:
        return HealthResult(
            reachable=False,
            authenticated=False,
            summary=f"nicht erreichbar: {exc.context.summary or exc.context.error_kind}",
        )
    except OpnCockpitError as exc:
        # Catch-all für unerwartete API-Fehler (z. B. 5xx vom Lab) —
        # wir melden konservativ "nicht authentifizierbar".
        return HealthResult(
            reachable=True,
            authenticated=False,
            summary=f"Antwort ungewöhnlich: {exc.context.error_kind}",
        )
