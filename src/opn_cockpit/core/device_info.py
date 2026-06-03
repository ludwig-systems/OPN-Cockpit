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

import base64
import binascii
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import NameOID

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
CERT_SEARCH_ENDPOINT = "/api/trust/cert/search"
CERT_GET_ENDPOINT_FMT = "/api/trust/cert/get/{uuid}"

_LOG = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Zertifikats-Inventur (v0.7 Safety-Net #3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CertificateEntry:
    """Ein einzelnes Zertifikat im OPNsense-Trust-Store.

    ``not_after_iso`` ist der UTC-ISO-String wie wir ihn parsen konnten;
    ``days_until_expiry`` ist eine Vorberechnung fuer die UI (positiv =
    laeuft noch, negativ = bereits abgelaufen, ``None`` = nicht parsbar).
    """

    uuid: str
    descr: str
    common_name: str
    issuer: str
    not_after_iso: str
    days_until_expiry: int | None
    in_use: bool


@dataclass(frozen=True, slots=True)
class CertificateStatus:
    """Zusammenfassender Zustand der Zertifikate eines Geraets."""

    reachable: bool
    authenticated: bool
    certs: tuple[CertificateEntry, ...]
    summary: str

    @property
    def soonest_days(self) -> int | None:
        """Geringste Anzahl Tage bis Ablauf ueber alle parsebaren Certs."""
        candidates = [c.days_until_expiry for c in self.certs if c.days_until_expiry is not None]
        return min(candidates) if candidates else None


def _extract_in_use(raw: Any) -> bool:
    """OPNsense liefert 'in_use' als '0'/'1'/0/1/bool. Akzeptiert alles."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return raw != 0
    if isinstance(raw, str):
        return raw.strip() in {"1", "true", "yes"}
    return False


def _first_str(row: dict[str, Any], *keys: str) -> str:
    """Liefert den ersten nicht-leeren String-Wert aus row[keys[i]].

    OPNsense's ``/cert/search`` listet nur ``uuid``/``descr``/``in_use`` direkt.
    Das ``/cert/get/<uuid>``-Schema schachtelt Felder unter ``cert``. Wir
    bleiben tolerant ueber mehrere Key-Varianten.
    """
    for k in keys:
        val = row.get(k)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            # OPNsense schachtelt z. B. issuer als {"CN": "..."} oder caref als
            # {"<uuid>": {"value": "...", "selected": 1}} - wir picken den ersten
            # erkannten Sub-Key heraus.
            for sub in ("CN", "commonName", "common_name", "cn", "value"):
                inner = val.get(sub)
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()
    return ""


def _decode_pem_blob(raw: Any) -> bytes | None:
    """OPNsense liefert das Cert im ``crt``-Feld als base64-PEM-Blob.

    Manche Releases liefern bereits den PEM-String roh ("-----BEGIN..."),
    andere wickeln den ganzen PEM-Text in base64. Beide Wege akzeptieren.
    Bei Decode-Fehler ``None``.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if "-----BEGIN" in text:
        return text.encode("ascii", errors="ignore")
    try:
        return base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError):
        return None


def _parse_cert_blob(pem_or_der: bytes) -> tuple[datetime | None, str, str]:
    """Liefert (not_after_utc, common_name, issuer_cn) aus PEM/DER-Bytes.

    Defensiv: Bei Parse-Fehlern ``(None, "", "")`` damit Cert-Inventur nie
    crasht. Issuer wird auf den Issuer-CN reduziert (kompaktere UI-Anzeige).
    """
    try:
        if b"-----BEGIN" in pem_or_der:
            cert = x509.load_pem_x509_certificate(pem_or_der, default_backend())
        else:
            cert = x509.load_der_x509_certificate(pem_or_der, default_backend())
    except (ValueError, TypeError) as exc:
        _LOG.debug("cert blob parse failed: %s", exc)
        return None, "", ""
    # cryptography>=42 hat not_valid_after_utc; aeltere Versionen nur das naive not_valid_after.
    not_after = (
        cert.not_valid_after_utc
        if hasattr(cert, "not_valid_after_utc")
        else cert.not_valid_after
    )
    if not_after.tzinfo is None:
        not_after = not_after.replace(tzinfo=UTC)

    def _name_cn(name: x509.Name) -> str:
        try:
            attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
            if attrs:
                value = attrs[0].value
                return value if isinstance(value, str) else value.decode("utf-8", errors="ignore")
        except (ValueError, IndexError):
            pass
        return name.rfc4514_string()

    return not_after, _name_cn(cert.subject), _name_cn(cert.issuer)


def fetch_certificates(
    client: HttpClient,
    target: HttpTarget,
    key: str,
    secret: str,
) -> CertificateStatus:
    """Liest die Zertifikats-Liste via ``/api/trust/cert/search``.

    Wirft niemals - schiebt Fehler in ``reachable`` / ``authenticated`` /
    ``summary``, damit Batch-Aufrufer ohne Try-Block weiterarbeiten koennen.
    """
    try:
        # ``current=1`` + grosser ``rowCount`` umgeht OPNsense's Default-
        # Pagination (haeufig 10 Eintraege), sonst wuerde ein frisch
        # erstelltes Cert auf einer Seite weiter hinten landen und der
        # Kachel-Badge nichts anzeigen.
        response = client.call(
            target, key, secret, "POST", CERT_SEARCH_ENDPOINT,
            json={"current": 1, "rowCount": 1000, "searchPhrase": ""},
        )
    except AuthError as exc:
        return CertificateStatus(
            reachable=True, authenticated=False, certs=(),
            summary=f"Auth abgelehnt: {exc.context.summary or 'Schluessel/Secret falsch'}",
        )
    except UnreachableError as exc:
        if exc.context.error_kind == "tls":
            reason = exc.context.summary or "Cert ungueltig"
            return CertificateStatus(
                reachable=True, authenticated=False, certs=(),
                summary=f"TLS-Verifikation fehlgeschlagen: {reason}",
            )
        return CertificateStatus(
            reachable=False, authenticated=False, certs=(),
            summary=f"nicht erreichbar: {exc.context.summary or exc.context.error_kind}",
        )
    except OpnCockpitError as exc:
        return CertificateStatus(
            reachable=True, authenticated=False, certs=(),
            summary=f"Antwort ungewoehnlich: {exc.context.error_kind}",
        )
    try:
        body = response.json()
    except ValueError:
        body = None
    rows: list[Any] = []
    if isinstance(body, dict):
        raw_rows = body.get("rows")
        if isinstance(raw_rows, list):
            rows = raw_rows
    elif isinstance(body, list):
        rows = body
    # OPNsense's /cert/search liefert nur uuid/descr/in_use - die Validity- und
    # Subject-Felder stehen im /cert/get/<uuid>-Endpoint, und dort steckt der
    # PEM-Blob im Feld ``crt`` (base64-kodiert oder roh-PEM, je nach Release).
    # Wir parsen das Blob client-seitig mit `cryptography.x509`, damit wir
    # unabhaengig von API-Schema-Aenderungen sind.
    now = datetime.now(UTC)
    entries = [
        _fetch_cert_detail(client, target, key, secret, row, now)
        for row in rows
        if isinstance(row, dict)
    ]
    return CertificateStatus(
        reachable=True, authenticated=True, certs=tuple(entries),
        summary=f"{len(entries)} Zertifikat(e) gefunden.",
    )


def _fetch_cert_detail(
    client: HttpClient,
    target: HttpTarget,
    key: str,
    secret: str,
    row: dict[str, Any],
    now: datetime,
) -> CertificateEntry:
    """Holt den Detail-Eintrag eines einzelnen Certs und parst dessen PEM."""
    uuid = _first_str(row, "uuid")
    descr = _first_str(row, "descr", "description")
    in_use = _extract_in_use(row.get("in_use"))
    not_after_dt: datetime | None = None
    common_name = ""
    issuer = ""
    if uuid:
        try:
            detail_resp = client.call(
                target, key, secret, "GET",
                CERT_GET_ENDPOINT_FMT.format(uuid=uuid),
            )
            detail_body = detail_resp.json()
        except (OpnCockpitError, ValueError):
            detail_body = None
        cert_node: Any = None
        if isinstance(detail_body, dict):
            cert_node = detail_body.get("cert")
            if not isinstance(cert_node, dict):
                cert_node = detail_body
        if isinstance(cert_node, dict):
            blob = _decode_pem_blob(cert_node.get("crt"))
            if blob is not None:
                not_after_dt, common_name, issuer = _parse_cert_blob(blob)
    not_after_iso = ""
    days: int | None = None
    if not_after_dt is not None:
        not_after_iso = not_after_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        days = (not_after_dt - now).days
    return CertificateEntry(
        uuid=uuid,
        descr=descr,
        common_name=common_name,
        issuer=issuer,
        not_after_iso=not_after_iso,
        days_until_expiry=days,
        in_use=in_use,
    )
