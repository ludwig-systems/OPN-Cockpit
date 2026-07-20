"""Karten-Widgets: CARP/HA, Interface-Link-Status, NTP.

Alle drei Fetch-Funktionen sind **read-only + defensive**:
* HTTP-Fehler landen in Reachable/Authenticated + summary — nie Exceptions.
* Wenn der Endpoint 404 antwortet (nicht alle OPNsense-Konfigs haben
  CARP-VIPs eingerichtet, ntp-Plugin ist optional, o.ae.), liefern wir
  einen ``unknown``-State statt zu crashen.
* Schema-Drift zwischen OPNsense-Versionen: wir sammeln nur wenige
  Felder und akzeptieren mehrere Auspraegungen (dict / list of items).

Cockpit ruft die drei Funktionen aus dem Heartbeat-Endpoint pro Kachel
auf — die Ergebnisse landen als kompakte Status-Zeilen in der
Gerate-Kachel-UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opn_cockpit.core.errors import (
    ApiError,
    AuthError,
    OpnCockpitError,
    UnreachableError,
    ValidationError,
)
from opn_cockpit.core.http_client import HttpClient, HttpTarget

# ---------------------------------------------------------------------------
# Endpoints (Best-Effort; genaue Auspraegung variiert je nach OPNsense-Version)
# ---------------------------------------------------------------------------

CARP_STATUS_ENDPOINT = "/api/diagnostics/interface/get_vip_status"
INTERFACES_INFO_ENDPOINT = "/api/interfaces/overview/interfacesInfo"
INTERFACES_TRAFFIC_ENDPOINT = "/api/diagnostics/traffic/interface"
INTERFACES_RELOAD_ENDPOINT_TEMPLATE = "/api/interfaces/overview/reloadInterface/{identifier}"
SYSTEM_INFO_ENDPOINT = "/api/diagnostics/system/systemInformation"

# Alternative NTP-Endpoints, die je nach OPNsense-Version verfuegbar sind.
# Wir probieren sie der Reihe nach durch, bis einer eine sinnvolle Antwort
# gibt. NTP-Status ist kein Kern-Feature in der OPNsense-Core-API — deshalb
# darf das Widget im UI auch dauerhaft "n/a" bleiben, und das ist keine
# Fehlfunktion.
NTP_STATUS_ENDPOINT_CANDIDATES = (
    "/api/diagnostics/systemhealth/getSystemHealth",
    "/api/diagnostics/system/systemInformation",
)


# ---------------------------------------------------------------------------
# Datentypen
# ---------------------------------------------------------------------------

STATE_OK = "ok"
STATE_WARN = "warn"
STATE_FAIL = "fail"
STATE_UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CarpStatus:
    """CARP/HA-Zustand einer OPNsense-Box.

    ``vip_count`` = Anzahl konfigurierter VIPs (0 = kein HA-Setup).
    ``master_count`` / ``backup_count`` / ``init_count`` = wie viele davon
    sind gerade in dem entsprechenden State.
    ``state`` = zusammengefasster UI-Zustand (``ok``/``warn``/``fail``/
    ``unknown``).
    ``maintenance_mode`` = OPNsense's "Persistent CARP Maintenance Mode"
    ist aktiviert. UI faerbt das gelb — HA-Failover ist bewusst
    deaktiviert.
    """

    reachable: bool
    authenticated: bool
    endpoint_available: bool     # False wenn 404 — Box hat keinen CARP-VIP-Endpoint
    state: str
    vip_count: int
    master_count: int
    backup_count: int
    init_count: int
    maintenance_mode: bool
    summary: str


@dataclass(frozen=True, slots=True)
class InterfacesStatus:
    """Physische Interfaces + Link-Status.

    ``total`` = Anzahl der Interfaces die OPNsense meldet. ``up_count`` /
    ``down_count`` = davon aktiv/inaktiv laut ``status``-Feld.
    ``down_names`` = Namen der down-Interfaces (max. 10 zur UI-Anzeige),
    fuer Tooltip auf der Kachel.
    """

    reachable: bool
    authenticated: bool
    endpoint_available: bool
    state: str
    total: int
    up_count: int
    down_count: int
    down_names: tuple[str, ...]
    summary: str


@dataclass(frozen=True, slots=True)
class NtpStatus:
    """NTP-Zeitsynchronisation.

    OPNsense hat keinen dedizierten JSON-API-Endpoint fuer NTP-Sync-Status.
    Wir nutzen ``systemInformation`` bzw. ``systemhealth`` als Naehe-
    rungswert: wenn dort ein sinnvolles ``date``-Feld ist und der Wert
    plausibel ist (nicht 1970, nicht in der Zukunft), gilt die Zeit als
    "wahrscheinlich synchron". Fuer echten NTP-Peer-State braeuchte man
    das ``os-ntp``-Plugin — das Widget zeigt in dem Fall ``unknown``
    und ist keine Fehlfunktion.
    """

    reachable: bool
    authenticated: bool
    endpoint_available: bool
    state: str
    system_time: str
    summary: str


# ---------------------------------------------------------------------------
# CARP / VIP-Status
# ---------------------------------------------------------------------------


def fetch_carp_status(
    client: HttpClient,
    target: HttpTarget,
    key: str,
    secret: str,
) -> CarpStatus:
    """Holt VIP-/CARP-Zustand."""
    reachable, authenticated, body, ep_available = _safe_fetch(
        client, target, key, secret, CARP_STATUS_ENDPOINT, method="POST",
    )
    if not reachable:
        return CarpStatus(
            reachable=False, authenticated=False,
            endpoint_available=False, state=STATE_UNKNOWN,
            vip_count=0, master_count=0, backup_count=0, init_count=0,
            maintenance_mode=False,
            summary="Box nicht erreichbar",
        )
    if not authenticated:
        return CarpStatus(
            reachable=True, authenticated=False,
            endpoint_available=False, state=STATE_UNKNOWN,
            vip_count=0, master_count=0, backup_count=0, init_count=0,
            maintenance_mode=False,
            summary="Auth abgelehnt",
        )
    if not ep_available:
        return CarpStatus(
            reachable=True, authenticated=True,
            endpoint_available=False, state=STATE_UNKNOWN,
            vip_count=0, master_count=0, backup_count=0, init_count=0,
            maintenance_mode=False,
            summary="kein CARP-Endpoint (OPNsense-Version?)",
        )

    # Response-Formen (je Version):
    # a) {"rows": [{"interface": "wan", "vhid": "1", "status": "MASTER"}, ...],
    #     "maintenancemode": "0"}
    # b) {"carp": {"maintenancemode": "0"},
    #     "rows": [...]}
    # c) direkte Liste (aeltere Versionen)
    rows = _extract_rows(body)
    maintenance = _extract_maintenance_mode(body)

    master = 0
    backup = 0
    init = 0
    for row in rows:
        raw_status = str(row.get("status", "")).strip().upper()
        if raw_status == "MASTER":
            master += 1
        elif raw_status == "BACKUP":
            backup += 1
        elif raw_status == "INIT":
            init += 1

    vip_count = len(rows)

    if vip_count == 0:
        return CarpStatus(
            reachable=True, authenticated=True,
            endpoint_available=True, state=STATE_UNKNOWN,
            vip_count=0, master_count=0, backup_count=0, init_count=0,
            maintenance_mode=maintenance,
            summary="Kein HA-Setup",
        )

    # UI-State:
    # * Alle Master -> ok (dies ist die Haupt-Node)
    # * Alle Backup -> ok (dies ist die Backup-Node, das ist auch valide)
    # * Mix aus Master/Backup -> warn (Split-Brain oder Uebergangszustand)
    # * INIT irgendwo -> warn (CARP negotiiert)
    # * Maintenance -> warn (bewusst deaktiviert)
    if maintenance:
        state = STATE_WARN
        summary = f"Maintenance-Mode ({vip_count} VIPs)"
    elif init > 0:
        state = STATE_WARN
        summary = f"{init} VIPs in INIT"
    elif master > 0 and backup > 0:
        state = STATE_WARN
        summary = f"{master} MASTER + {backup} BACKUP (Mix?)"
    elif master == vip_count:
        state = STATE_OK
        summary = f"MASTER ({vip_count} VIPs)"
    elif backup == vip_count:
        state = STATE_OK
        summary = f"BACKUP ({vip_count} VIPs)"
    else:
        state = STATE_WARN
        summary = f"{master} MASTER, {backup} BACKUP, {init} INIT"

    return CarpStatus(
        reachable=True, authenticated=True,
        endpoint_available=True, state=state,
        vip_count=vip_count, master_count=master,
        backup_count=backup, init_count=init,
        maintenance_mode=maintenance,
        summary=summary,
    )


def _extract_maintenance_mode(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    for key in ("maintenancemode", "maintenance_mode", "carp_maintenancemode"):
        if key in body:
            return _as_bool(body.get(key))
    carp = body.get("carp")
    if isinstance(carp, dict):
        for key in ("maintenancemode", "maintenance_mode"):
            if key in carp:
                return _as_bool(carp.get(key))
    return False


# ---------------------------------------------------------------------------
# Interfaces (Link-Status)
# ---------------------------------------------------------------------------


def fetch_interfaces_status(
    client: HttpClient,
    target: HttpTarget,
    key: str,
    secret: str,
) -> InterfacesStatus:
    """Holt Interface-Uebersicht mit Link-Status.

    Response-Form (OPNsense 25.x/26.x):
    ``{"rows": [{"device": "em0", "identifier": "wan", "status": "up",
                 "link": "up", "description": "WAN", ...}, ...], ...}``

    Aeltere Versionen liefern eine Liste direkt (ohne ``rows``-Wrapper).
    """
    reachable, authenticated, body, ep_available = _safe_fetch(
        client, target, key, secret, INTERFACES_INFO_ENDPOINT, method="POST",
    )
    if not reachable:
        return InterfacesStatus(
            reachable=False, authenticated=False,
            endpoint_available=False, state=STATE_UNKNOWN,
            total=0, up_count=0, down_count=0, down_names=(),
            summary="Box nicht erreichbar",
        )
    if not authenticated:
        return InterfacesStatus(
            reachable=True, authenticated=False,
            endpoint_available=False, state=STATE_UNKNOWN,
            total=0, up_count=0, down_count=0, down_names=(),
            summary="Auth abgelehnt",
        )
    if not ep_available:
        return InterfacesStatus(
            reachable=True, authenticated=True,
            endpoint_available=False, state=STATE_UNKNOWN,
            total=0, up_count=0, down_count=0, down_names=(),
            summary="Kein Interfaces-Endpoint",
        )

    rows = _extract_rows(body)
    if not rows:
        return InterfacesStatus(
            reachable=True, authenticated=True,
            endpoint_available=True, state=STATE_UNKNOWN,
            total=0, up_count=0, down_count=0, down_names=(),
            summary="Keine Interfaces gemeldet",
        )

    up = 0
    down = 0
    down_names: list[str] = []
    for row in rows:
        # OPNsense hat "enabled" (Config) + "status" (Link). Wir zaehlen
        # nur enabled Interfaces mit — deaktivierte Ports sollen nicht
        # rot leuchten.
        enabled = _as_bool(row.get("enabled", True))
        if not enabled:
            continue
        # Prüfen ob die Box das Interface als "up" meldet.
        status_val = str(row.get("status", "")).strip().lower()
        link_val = str(row.get("link", "")).strip().lower()
        is_up = (
            status_val in {"up", "1", "active", "assoc"}
            or link_val in {"up", "1", "active"}
        )
        # Label bevorzugt der User-Facing "identifier" (wan/lan/opt1),
        # sonst description, sonst device (em0).
        label = (
            str(row.get("identifier", ""))
            or str(row.get("description", ""))
            or str(row.get("device", ""))
        ).strip() or "?"
        if is_up:
            up += 1
        else:
            down += 1
            if len(down_names) < 10:
                down_names.append(label)

    total = up + down
    if total == 0:
        state = STATE_UNKNOWN
        summary = "Keine aktiven Interfaces"
    elif down == 0:
        state = STATE_OK
        summary = f"{up}/{total} up"
    else:
        # Down-Interfaces sind sichtbar aber nicht automatisch kritisch —
        # LAN-Ohne-Kabel ist Normalzustand bei vielen Boxen. Wir markieren
        # gelb (warn) wenn mehr als 25% down sind, sonst nur informativ ok.
        ratio_down = down / total
        state = STATE_WARN if ratio_down > 0.25 else STATE_OK
        summary = f"{up}/{total} up ({down} down)"

    return InterfacesStatus(
        reachable=True, authenticated=True,
        endpoint_available=True, state=state,
        total=total, up_count=up, down_count=down,
        down_names=tuple(down_names),
        summary=summary,
    )


# ---------------------------------------------------------------------------
# NTP / Zeit-Synchronisation
# ---------------------------------------------------------------------------


def fetch_ntp_status(
    client: HttpClient,
    target: HttpTarget,
    key: str,
    secret: str,
) -> NtpStatus:
    """Best-Effort NTP-Status via System-Information.

    Kein dedizierter OPNsense-JSON-Endpoint fuer NTP-Sync-State — wir
    machen es indirekt: ``systemInformation`` liefert typisch ``date``
    und/oder ``uptime``. Wenn ``date`` da ist und plausibel (nicht in
    ferner Zukunft, nicht Epoch), gilt die Zeit als "vermutlich okay".
    Fuer echten Sync-Status muss der User das ``os-ntp``-Plugin
    installieren — Cockpit meldet dann trotzdem den best-effort Zustand.
    """
    for endpoint in NTP_STATUS_ENDPOINT_CANDIDATES:
        reachable, authenticated, body, ep_available = _safe_fetch(
            client, target, key, secret, endpoint, method="POST",
        )
        if not reachable:
            return NtpStatus(
                reachable=False, authenticated=False,
                endpoint_available=False, state=STATE_UNKNOWN,
                system_time="", summary="Box nicht erreichbar",
            )
        if not authenticated:
            return NtpStatus(
                reachable=True, authenticated=False,
                endpoint_available=False, state=STATE_UNKNOWN,
                system_time="", summary="Auth abgelehnt",
            )
        if not ep_available:
            # naechsten Kandidaten probieren
            continue

        system_time = _extract_system_time(body)
        if system_time:
            return NtpStatus(
                reachable=True, authenticated=True,
                endpoint_available=True, state=STATE_OK,
                system_time=system_time,
                summary=f"Zeit: {system_time}",
            )
        # Endpoint da, aber kein date/time extrahierbar -> unknown
        return NtpStatus(
            reachable=True, authenticated=True,
            endpoint_available=True, state=STATE_UNKNOWN,
            system_time="", summary="System-Zeit nicht auslesbar",
        )

    # Kein Kandidat lieferte einen brauchbaren Endpoint.
    return NtpStatus(
        reachable=True, authenticated=True,
        endpoint_available=False, state=STATE_UNKNOWN,
        system_time="", summary="Kein NTP-Info-Endpoint",
    )


def _extract_system_time(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    for key in ("date", "system_time", "datetime", "time"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # Verschachtelte Formen:
    system = body.get("system")
    if isinstance(system, dict):
        for key in ("date", "time"):
            val = system.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


# ---------------------------------------------------------------------------
# Gemeinsame Helfer
# ---------------------------------------------------------------------------


def _safe_fetch(
    client: HttpClient,
    target: HttpTarget,
    key: str,
    secret: str,
    endpoint: str,
    *,
    method: str = "POST",
) -> tuple[bool, bool, Any, bool]:
    """Ruft einen Endpoint auf und fangt Fehler ab.

    Return: ``(reachable, authenticated, body, endpoint_available)``.
    ``endpoint_available=False`` bei HTTP 404 — der Endpoint existiert
    in dieser OPNsense-Version nicht, ist aber sonst technisch alles OK.
    """
    try:
        json_body: dict[str, Any] | None = {} if method.upper() == "POST" else None
        response = client.call(target, key, secret, method, endpoint, json=json_body)
    except AuthError:
        return True, False, None, False
    except UnreachableError as exc:
        # Manche Pfade verpacken 404 als UnreachableError.
        if getattr(exc.context, "status_code", None) == 404:
            return True, True, None, False
        return False, False, None, False
    except ValidationError as exc:
        # HttpClient wirft 4xx (ausser 401/403) als ValidationError -
        # 404 bedeutet nur "Endpoint gibt's hier nicht", nicht Auth-Problem.
        if getattr(exc.context, "status_code", None) == 404:
            return True, True, None, False
        # Andere 4xx (400/etc) - Endpoint da, aber wir haben was falsch
        # gemacht. Auth ist trotzdem valide.
        return True, True, None, True
    except ApiError as exc:
        if getattr(exc.context, "status_code", None) == 404:
            return True, True, None, False
        # 5xx: erreichbar aber Server-Fehler.
        return True, True, None, True
    except OpnCockpitError:
        return True, False, None, False

    if response.status_code == 404:
        return True, True, None, False

    try:
        body = response.json()
    except ValueError:
        body = None
    return True, True, body, True


def _extract_rows(body: Any) -> list[dict[str, Any]]:
    """OPNsense-Response-Konvention: entweder ``{"rows": [...]}`` oder
    Liste direkt oder ``{"items": [...]}``.
    """
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if not isinstance(body, dict):
        return []
    for key in ("rows", "items", "interface"):
        val = body.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
        if isinstance(val, dict):
            # Manche Endpoints liefern ein Dict wo die Werte je ein Row-Dict sind.
            return [v for v in val.values() if isinstance(v, dict)]
    return []


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "up"}
    return False


# ---------------------------------------------------------------------------
# Interfaces-Detail (fuer Device-Modal-Tab)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InterfaceDetail:
    """Detail-Info pro Interface fuer den Device-Modal-Tab.

    Unterscheidet Admin-Status (Config-Ebene, ``enabled``) von Link-
    Status (Physik-Ebene, ``link_up``). Beides ist fuer die
    Statuspunkt-Farbe relevant:
    * ``enabled=false``     -> grau (deaktiviert, aus)
    * ``enabled=true`` + ``link_up=true``  -> gruen (voll aktiv)
    * ``enabled=true`` + ``link_up=false`` -> gelb (Kabel raus / no-carrier)
    """

    identifier: str        # "wan", "lan", "opt1"
    description: str       # OPNsense-Description ("HQ WAN Uplink")
    device: str            # OS-Device-Name ("em0", "igb1", "vtnet0")
    enabled: bool          # Admin-Up
    link_up: bool          # Physik-Link
    status_raw: str        # Rohes Status-Wort von OPNsense (fuer Tooltip)
    ipv4: str              # Primaere IPv4-Adresse
    ipv4_subnetbits: int   # /24, /29, ...
    ipv6: str              # Primaere IPv6-Adresse
    ipv6_subnetbits: int
    mtu: str               # OPNsense liefert das als String — wir uebernehmen 1:1
    macaddr: str
    media: str             # z. B. "1000baseTX <full-duplex>"
    # Traffic-Counter (kumulativ seit Interface-Up). 0 wenn OPNsense keinen
    # Traffic-Endpoint bietet oder das OS-Device dort nicht auftaucht.
    bytes_received: int = 0
    bytes_transmitted: int = 0
    packets_received: int = 0
    packets_transmitted: int = 0


@dataclass(frozen=True, slots=True)
class InterfacesDetailResult:
    reachable: bool
    authenticated: bool
    endpoint_available: bool
    interfaces: tuple[InterfaceDetail, ...]
    summary: str


def _extract_first_ip(row: dict[str, Any], key: str) -> tuple[str, int]:
    """Zieht die erste IPv4/IPv6-Adresse aus dem ipv4/ipv6-Feld.

    OPNsense-Response-Variante 1: ``ipv4: [{"ipaddr": "1.2.3.4",
    "subnetbits": 24}]`` (Liste).
    Variante 2: direkt String ``"1.2.3.4/24"``.
    Variante 3: Dict mit ``address``/``prefix``.
    """
    raw = row.get(key)
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, dict):
            ip = str(first.get("ipaddr", first.get("address", ""))).strip()
            bits_raw = first.get("subnetbits", first.get("prefix", ""))
            try:
                bits = int(str(bits_raw)) if bits_raw not in ("", None) else 0
            except (TypeError, ValueError):
                bits = 0
            return ip, bits
    if isinstance(raw, dict):
        ip = str(raw.get("ipaddr", raw.get("address", ""))).strip()
        bits_raw = raw.get("subnetbits", raw.get("prefix", ""))
        try:
            bits = int(str(bits_raw)) if bits_raw not in ("", None) else 0
        except (TypeError, ValueError):
            bits = 0
        return ip, bits
    if isinstance(raw, str) and "/" in raw:
        ip_part, _, bits_part = raw.partition("/")
        try:
            return ip_part.strip(), int(bits_part)
        except ValueError:
            return ip_part.strip(), 0
    if isinstance(raw, str) and raw.strip():
        return raw.strip(), 0
    return "", 0


def _fetch_traffic_counters(
    client: HttpClient,
    target: HttpTarget,
    key: str,
    secret: str,
) -> dict[str, tuple[int, int, int, int]]:
    """Liefert pro OS-Device-Name (em0, igb1, vtnet0) ein Tupel
    (bytes_rx, bytes_tx, packets_rx, packets_tx).

    Best-Effort: fehlt der Endpoint, kommt ein leeres Dict zurueck und
    der Aufrufer zeigt die Interfaces halt ohne Traffic-Zahlen. Wir
    reissen den ganzen Interface-Tab nicht ab weil ein Nebenwidget
    kaputt ist.
    """
    reachable, authenticated, body, ep_available = _safe_fetch(
        client, target, key, secret, INTERFACES_TRAFFIC_ENDPOINT, method="POST",
    )
    if not reachable or not authenticated or not ep_available:
        return {}

    ifaces = body.get("interfaces") if isinstance(body, dict) else None
    if not isinstance(ifaces, dict):
        return {}

    def _int(value: Any) -> int:
        try:
            return int(str(value or "0"))
        except (TypeError, ValueError):
            return 0

    out: dict[str, tuple[int, int, int, int]] = {}
    for dev_name, dev_stats in ifaces.items():
        if not isinstance(dev_stats, dict):
            continue
        # OPNsense-Schluesselvariante: "bytes received" (Space)
        # oder in aelteren Builds "bytes_received" — beide abfangen.
        bytes_rx = _int(dev_stats.get(
            "bytes received", dev_stats.get("bytes_received", 0),
        ))
        bytes_tx = _int(dev_stats.get(
            "bytes transmitted", dev_stats.get("bytes_transmitted", 0),
        ))
        packets_rx = _int(dev_stats.get(
            "packets received", dev_stats.get("packets_received", 0),
        ))
        packets_tx = _int(dev_stats.get(
            "packets transmitted", dev_stats.get("packets_transmitted", 0),
        ))
        out[str(dev_name)] = (bytes_rx, bytes_tx, packets_rx, packets_tx)
    return out


def fetch_interfaces_detailed(
    client: HttpClient,
    target: HttpTarget,
    key: str,
    secret: str,
) -> InterfacesDetailResult:
    """Vollstaendige Interface-Liste mit IP/MTU/MAC/Link-Status + Traffic.

    Nutzt denselben Endpoint wie ``fetch_interfaces_status``, extrahiert
    aber alle sinnvollen Felder pro Zeile — fuer den Device-Modal-Tab.
    Danach ein zweiter Best-Effort-Call an den Traffic-Endpoint fuer
    RX/TX-Counter (fehlt der Endpoint, bleiben die Zahlen bei 0).
    """
    reachable, authenticated, body, ep_available = _safe_fetch(
        client, target, key, secret, INTERFACES_INFO_ENDPOINT, method="POST",
    )
    if not reachable or not authenticated or not ep_available:
        summary = (
            "Box nicht erreichbar" if not reachable
            else "Auth abgelehnt" if not authenticated
            else "Interfaces-Endpoint nicht verfuegbar"
        )
        return InterfacesDetailResult(
            reachable=reachable, authenticated=authenticated,
            endpoint_available=ep_available,
            interfaces=(), summary=summary,
        )

    traffic_by_device = _fetch_traffic_counters(client, target, key, secret)

    rows = _extract_rows(body)
    entries: list[InterfaceDetail] = []
    for row in rows:
        identifier = (
            str(row.get("identifier", ""))
            or str(row.get("name", ""))
        ).strip()
        device = str(row.get("device", "")).strip()
        # Zeilen ohne Identifier UND ohne Device sind Datenmuell.
        if not identifier and not device:
            continue

        enabled = _as_bool(row.get("enabled", True))
        status_val = str(row.get("status", "")).strip().lower()
        link_val = str(row.get("link", "")).strip().lower()
        link_up = (
            status_val in {"up", "1", "active", "assoc"}
            or link_val in {"up", "1", "active"}
        )

        ipv4, ipv4_bits = _extract_first_ip(row, "ipv4")
        ipv6, ipv6_bits = _extract_first_ip(row, "ipv6")

        rx_b, tx_b, rx_p, tx_p = traffic_by_device.get(device, (0, 0, 0, 0))

        entries.append(InterfaceDetail(
            identifier=identifier,
            description=str(row.get("description", "")).strip(),
            device=device,
            enabled=enabled,
            link_up=link_up,
            status_raw=str(row.get("status", "")).strip(),
            ipv4=ipv4,
            ipv4_subnetbits=ipv4_bits,
            ipv6=ipv6,
            ipv6_subnetbits=ipv6_bits,
            mtu=str(row.get("mtu", "")).strip(),
            macaddr=str(row.get("macaddr", "")).strip(),
            media=str(row.get("media", "")).strip(),
            bytes_received=rx_b,
            bytes_transmitted=tx_b,
            packets_received=rx_p,
            packets_transmitted=tx_p,
        ))

    # Nach Identifier alphabetisch sortieren (wan, lan, opt1..); Interfaces
    # ohne Identifier ans Ende.
    entries.sort(key=lambda e: (not e.identifier, e.identifier.lower(), e.device))
    return InterfacesDetailResult(
        reachable=True, authenticated=True,
        endpoint_available=True,
        interfaces=tuple(entries),
        summary=f"{len(entries)} Interface(s) live geladen.",
    )


__all__ = [
    "CARP_STATUS_ENDPOINT",
    "CarpStatus",
    "INTERFACES_INFO_ENDPOINT",
    "INTERFACES_RELOAD_ENDPOINT_TEMPLATE",
    "INTERFACES_TRAFFIC_ENDPOINT",
    "InterfaceDetail",
    "InterfacesDetailResult",
    "InterfacesStatus",
    "NTP_STATUS_ENDPOINT_CANDIDATES",
    "NtpStatus",
    "STATE_FAIL",
    "STATE_OK",
    "STATE_UNKNOWN",
    "STATE_WARN",
    "SYSTEM_INFO_ENDPOINT",
    "fetch_carp_status",
    "fetch_interfaces_detailed",
    "fetch_interfaces_status",
    "fetch_ntp_status",
]
