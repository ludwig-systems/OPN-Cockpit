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

import socket
from dataclasses import dataclass

from opn_cockpit.core.errors import AuthError, OpnCockpitError, UnreachableError
from opn_cockpit.core.http_client import HttpClient, HttpTarget

HEALTH_ENDPOINT = "/api/core/menu/tree"
DEFAULT_TCP_PROBE_TIMEOUT_S = 3.0


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
        # TLS-Cert-Probleme verdienen einen eigenen Wortlaut - Host ist
        # technisch erreichbar, wir vertrauen ihm aber nicht. Sonst denkt der
        # Admin "Netzwerk weg" obwohl in Wirklichkeit das Zertifikat fehlt.
        if exc.context.error_kind == "tls":
            return HealthResult(
                reachable=True,
                authenticated=False,
                summary=(
                    f"TLS-Verifikation fehlgeschlagen: "
                    f"{exc.context.summary or 'Zertifikat nicht vertrauenswuerdig'}. "
                    "Fixe das Zertifikat auf der OPNsense oder schalte "
                    "TLS-Pruefung fuer dieses Geraet ab (Risiko-Markierung)."
                ),
            )
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


def tcp_probe(
    host: str,
    port: int,
    *,
    timeout_s: float = DEFAULT_TCP_PROBE_TIMEOUT_S,
) -> bool:
    """Schneller TCP-Connect ohne HTTP/Auth.

    Wird vom Hintergrund-Heartbeat im GUI-Inventar verwendet, um pro Gerät
    in 1-3 Sekunden zu klären, ob der API-Port überhaupt erreichbar ist.
    Bewusst KEIN HTTP-Aufruf — der Heartbeat soll keine Last auf den
    OPNsense-Endpoints erzeugen und keine Auth-Versuche im Audit-Log
    der Box hinterlassen.

    Liefert ``True``, wenn der 3-Way-Handshake innerhalb von
    ``timeout_s`` zustande kommt; sonst ``False``. Wirft niemals — Fehler
    werden zu ``False`` reduziert, weil die Aufrufer (UI-Heartbeat) nur
    am Boolean-Ergebnis interessiert sind.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except (TimeoutError, OSError):
        return False
