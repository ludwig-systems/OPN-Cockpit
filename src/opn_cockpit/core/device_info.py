"""Lesende Operationen pro Geraet: Firmware-Status + Konfig-Backup.

Beide Funktionen sind kostenguenstig (ein einzelner HTTP-GET) und werden
analog ``health.check_device`` ohne Audit-/Session-Konzepte gehalten —
die Web-/CLI-Schicht wickelt das Audit-Eintrag- und User-Feedback selbst.

``fetch_firmware_status`` ruft ``/api/core/firmware/status`` auf und
extrahiert defensiv die OPNsense-Versionsnummer und einen booleschen
"Update verfuegbar?"-Indikator. OPNsense aendert die Schema-Form
zwischen Releases — wir akzeptieren mehrere bekannte Auspraegungen
und liefern bei voelliger Schema-Aenderung ``"unknown"`` zurueck statt
zu crashen.

``download_backup`` ruft ``/api/core/backup/download/this`` auf und
liefert die rohen XML-Bytes der aktuellen Geraete-Konfiguration. Der
Aufrufer entscheidet ueber Speichern/Streamen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opn_cockpit.core.errors import (
    ApiError,
    AuthError,
    OpnCockpitError,
    UnreachableError,
    make_context,
)
from opn_cockpit.core.http_client import HttpClient, HttpTarget

FIRMWARE_STATUS_ENDPOINT = "/api/core/firmware/status"
FIRMWARE_CHECK_ENDPOINT = "/api/core/firmware/check"
BACKUP_DOWNLOAD_ENDPOINT = "/api/core/backup/download/this"


@dataclass(frozen=True, slots=True)
class FirmwareStatus:
    """OPNsense-Firmware-Status eines Geraets.

    ``reachable`` / ``authenticated`` getrennt damit der Aufrufer die UI
    unterschiedlich faerben kann (nicht erreichbar vs. Auth-Problem vs.
    Antwort-Schema unbekannt).

    ``version`` ist der String wie OPNsense ihn meldet, z. B. ``"25.7.1"``.
    Default ``"unknown"`` wenn die Antwort nicht parsbar war.

    ``status`` ist das von OPNsense gemeldete Update-Status-Wort —
    typische Werte: ``none``, ``update``, ``upgrade``, ``ok``. Fuer
    Frontend-Anzeigen nutzen wir ``update_available`` als bool.

    ``new_version`` ist die Zielversion, falls OPNsense ein Update meldet,
    sonst leer. Ueberblickskachel zeigt das als ``Update: v25.7.2``.

    ``status_msg`` ist OPNsense's eigene Beschreibung des Update-Status
    ("There are 12 packages to be upgraded.") - fuer Tooltip auf der Karte.
    """

    reachable: bool
    authenticated: bool
    version: str
    status: str
    update_available: bool
    summary: str
    new_version: str = ""
    status_msg: str = ""


def _extract_version(body: Any) -> str:
    """Robust gegen die paar bekannten Schema-Varianten von OPNsense."""
    if not isinstance(body, dict):
        return "unknown"
    # Variante 1 (neuere Releases): {"product": {"product_version": "25.7.1", ...}}
    product = body.get("product")
    if isinstance(product, dict):
        cand = product.get("product_version") or product.get("version")
        if isinstance(cand, str) and cand.strip():
            return cand.strip()
    # Variante 2: direktes Top-Level-Feld
    for key in ("product_version", "version"):
        cand = body.get(key)
        if isinstance(cand, str) and cand.strip():
            return cand.strip()
    return "unknown"


def _extract_status(body: Any) -> tuple[str, bool]:
    """Liefert (status-string, update_available-bool)."""
    if not isinstance(body, dict):
        return "unknown", False
    status_raw = body.get("status")
    if not isinstance(status_raw, str):
        # Manche Versionen haben das Status-Wort in product oder upgrade
        upgrade_pkgs = body.get("upgrade_packages")
        if isinstance(upgrade_pkgs, list) and upgrade_pkgs:
            return "update", True
        return "unknown", False
    status = status_raw.strip().lower()
    # `none` / `ok` = aktuell, `update` / `upgrade` = etwas verfuegbar.
    update_available = status in {"update", "upgrade"}
    return status, update_available


def _extract_new_version(body: Any) -> str:
    """Zielversion eines verfuegbaren Updates, oder leer.

    Mehrere bekannte Quellen, in Praeferenz-Reihenfolge:

    1. ``product.product_target_version`` (24.x / 25.x)
    2. ``upgrade_packages[*].new`` fuer das ``opnsense``-Hauptpaket
    3. ``product.product_nickname`` als letzte Fallback-Zeile
    """
    if not isinstance(body, dict):
        return ""
    product = body.get("product")
    if isinstance(product, dict):
        target = product.get("product_target_version") or product.get("target_version")
        if isinstance(target, str) and target.strip():
            return target.strip()
    upgrade_pkgs = body.get("upgrade_packages")
    if isinstance(upgrade_pkgs, list):
        for entry in upgrade_pkgs:
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or "").strip().lower()
            if name == "opnsense":
                new = entry.get("new")
                if isinstance(new, str) and new.strip():
                    return new.strip()
    return ""


def _extract_status_msg(body: Any) -> str:
    """Frei-Text-Beschreibung des Update-Status, fuer Tooltip auf der Karte."""
    if not isinstance(body, dict):
        return ""
    raw = body.get("status_msg")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return ""


def fetch_firmware_status(
    client: HttpClient,
    target: HttpTarget,
    key: str,
    secret: str,
) -> FirmwareStatus:
    """Holt den Firmware-Status eines Geraets.

    Wirft niemals — schiebt Fehler in die ``reachable``/``authenticated``/
    ``summary``-Felder, damit der Caller in Batch-Aufrufen ohne Try-Block
    weiterarbeiten kann.
    """
    try:
        response = client.call(target, key, secret, "GET", FIRMWARE_STATUS_ENDPOINT)
    except AuthError as exc:
        return FirmwareStatus(
            reachable=True, authenticated=False,
            version="unknown", status="unknown", update_available=False,
            summary=f"Auth abgelehnt: {exc.context.summary or 'Schluessel/Secret falsch'}",
        )
    except UnreachableError as exc:
        if exc.context.error_kind == "tls":
            tls_reason = exc.context.summary or "Cert ungueltig"
            return FirmwareStatus(
                reachable=True, authenticated=False,
                version="unknown", status="unknown", update_available=False,
                summary=f"TLS-Verifikation fehlgeschlagen: {tls_reason}",
            )
        return FirmwareStatus(
            reachable=False, authenticated=False,
            version="unknown", status="unknown", update_available=False,
            summary=f"nicht erreichbar: {exc.context.summary or exc.context.error_kind}",
        )
    except OpnCockpitError as exc:
        return FirmwareStatus(
            reachable=True, authenticated=False,
            version="unknown", status="unknown", update_available=False,
            summary=f"Antwort ungewoehnlich: {exc.context.error_kind}",
        )
    try:
        body = response.json()
    except ValueError:
        body = None
    version = _extract_version(body)
    status_word, update_available = _extract_status(body)
    new_version = _extract_new_version(body) if update_available else ""
    status_msg = _extract_status_msg(body)
    # Summary kompakt mit Zielversion wenn bekannt, sonst Generik.
    if update_available:
        suffix = (
            f" — Update v{new_version} verfuegbar" if new_version
            else " — Update verfuegbar"
        )
    else:
        suffix = ""
    return FirmwareStatus(
        reachable=True, authenticated=True,
        version=version, status=status_word,
        update_available=update_available,
        summary=f"v{version}{suffix}",
        new_version=new_version,
        status_msg=status_msg,
    )


def trigger_firmware_check(
    client: HttpClient,
    target: HttpTarget,
    key: str,
    secret: str,
) -> tuple[bool, str]:
    """Stoesst auf OPNsense den "Check for Updates"-Vorgang an.

    Aequivalent zum Klick auf "Check for updates" im OPNsense-Firmware-View.
    Der Vorgang ist auf OPNsense-Seite asynchron - die Antwort kommt
    typischerweise sofort, die eigentliche Aktualisierung der Status-Cache
    laeuft fuer 5-30 Sekunden im Hintergrund.

    Liefert ``(success, message)``. ``success=False`` heisst entweder Auth/
    Netzwerkfehler oder OPNsense hat die Aktion abgelehnt. ``message`` ist
    eine kurze Diagnose fuer das Frontend.
    """
    try:
        response = client.call(target, key, secret, "POST", FIRMWARE_CHECK_ENDPOINT)
    except AuthError as exc:
        return False, f"Auth abgelehnt: {exc.context.summary or 'Schluessel/Secret falsch'}"
    except UnreachableError as exc:
        if exc.context.error_kind == "tls":
            reason = exc.context.summary or "Cert ungueltig"
            return False, f"TLS-Verifikation fehlgeschlagen: {reason}"
        return False, f"nicht erreichbar: {exc.context.summary or exc.context.error_kind}"
    except OpnCockpitError as exc:
        return False, f"Antwort ungewoehnlich: {exc.context.error_kind}"
    # OPNsense liefert typischerweise {"status": "ok"}; wir akzeptieren
    # alles im 2xx-Bereich (HttpClient hat das schon gefiltert) als OK.
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        status_str = body.get("status")
        if isinstance(status_str, str) and status_str.lower() in {"ok", "running"}:
            return True, "Check angestossen."
    return True, "Check angestossen."


def download_backup(
    client: HttpClient,
    target: HttpTarget,
    key: str,
    secret: str,
) -> bytes:
    """Laedt die aktuelle Konfiguration als XML-Bytes herunter.

    Wirft ``UnreachableError`` / ``AuthError`` / ``ApiError`` durch — der
    Web-Layer wandelt das in HTTP-Status um. Leere Antworten gelten als
    Fehler, weil OPNsense bei korrektem GET immer mindestens ein
    ``<opnsense>``-Root liefert.
    """
    response = client.call(target, key, secret, "GET", BACKUP_DOWNLOAD_ENDPOINT)
    content = response.content
    if not content:
        raise ApiError(
            "OPNsense lieferte leere Backup-Antwort.",
            context=make_context(
                host=target.host,
                port=target.port,
                method="GET",
                path=BACKUP_DOWNLOAD_ENDPOINT,
                status_code=response.status_code,
                error_kind="backup_empty",
            ),
        )
    return content
