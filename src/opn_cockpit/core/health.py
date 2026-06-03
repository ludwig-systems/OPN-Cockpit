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
import subprocess
import sys
from dataclasses import dataclass

from opn_cockpit.core.errors import AuthError, OpnCockpitError, UnreachableError
from opn_cockpit.core.http_client import HttpClient, HttpTarget

HEALTH_ENDPOINT = "/api/core/menu/tree"
DEFAULT_TCP_PROBE_TIMEOUT_S = 3.0
DEFAULT_ICMP_PROBE_TIMEOUT_S = 2.0

# Klassen von Connection-Fehlern, bei denen ein zusaetzlicher ICMP-Probe
# diagnostisch hilft: Host ist evtl. erreichbar, nur der TCP-Port nicht.
_PROBE_WORTHY_ERROR_KINDS = frozenset({
    "connect_timeout",
    "connect_refused",
    "connection_error",
    "network",
})


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
        # Layer-4-Diagnose: bei TCP-Timeout / Refused / Connection-Error
        # zusaetzlich ICMP-Probe. Wenn der Host pingbar ist, ist das Problem
        # nicht "Box down" sondern "Port zu" - Firewall, asymmetrisches
        # Routing oder Dienst nicht aktiv. Lessons-Learned aus der XL9-Diag-
        # Session: das Symptom "Timeout" allein war frueher unterspezifiziert.
        if exc.context.error_kind in _PROBE_WORTHY_ERROR_KINDS and icmp_probe(target.host):
            return HealthResult(
                reachable=True,
                authenticated=False,
                summary=(
                    f"Host antwortet auf Ping, aber Port {target.port} ist zu: "
                    f"{exc.context.summary or exc.context.error_kind}. "
                    "Pruefe Firewall-Regel, asymmetrisches Routing oder ob die "
                    "OPNsense-WebGUI auf dem konfigurierten Interface laeuft."
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


def icmp_probe(
    host: str,
    *,
    timeout_s: float = DEFAULT_ICMP_PROBE_TIMEOUT_S,
) -> bool:
    """ICMP-Ping-Probe via system ``ping`` (kein Raw-Socket noetig).

    Cross-platform: Windows nutzt ``ping -n 1 -w <ms>``, Unix/BSD
    ``ping -c 1 -W <s>``. Liefert True wenn ein Echo-Reply innerhalb
    von ``timeout_s`` ankam, sonst False. Wirft niemals - jeder
    Subprozess-Fehler wird als False gewertet.

    Zweck: bei TCP-Timeout zusaetzlich pruefen ob der Host wenigstens
    auf Layer 3 antwortet. Wenn ja, ist der Port zu (Firewall / Service
    aus / asymmetrisches Routing) - das beheben ist eine andere Sorte
    von Arbeit als "Host ist down".
    """
    if not host:
        return False
    if sys.platform == "win32":
        # Windows: -n Anzahl, -w timeout in ms, -4 = IPv4 erzwingen
        cmd = ["ping", "-n", "1", "-w", str(int(timeout_s * 1000)), "-4", host]
    else:
        # Linux/BSD: -c Anzahl, -W timeout in Sekunden
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_s))), host]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout_s + 1.0,
            check=False,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


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
