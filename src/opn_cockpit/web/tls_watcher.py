"""TlsWatcher: prueft periodisch das aktive Server-Cert auf Ablauf.

Setzt einen einfachen Zustandsdict auf ``app.state.tls_state``, den
das UI per Polling-Endpoint abholt. Bei Ablauf-Uebergaengen schreibt
er Audit-Events; bei bereits abgelaufenem Auto-Cert triggert er einen
Auto-Restart (Voraussetzung: Service-Mode).

Design-Constraints:

* Watcher liest ausschliesslich Filesystem + AppSettings — kein Vault-
  Zugriff noetig, laeuft auch ohne User-Session.
* Tick alle 6 Stunden (``DEFAULT_TICK_S = 21600``). Erste Auswertung
  10 s nach Start damit ``app.state.tls_state`` sofort da ist fuer
  Polling.
* Custom-Cert-Ablauf: nur Banner + Audit, KEINE Auto-Regeneration
  (User entscheidet das bewusst).
* Auto-Cert-Ablauf: Regeneration + Restart (sonst ist der Server
  unerreichbar).

Zustaende in ``tls_state``:

    {
        "status":            "ok" | "warn" | "critical" | "expired",
        "cert_type":         "custom" | "auto" | "none",
        "days_left":         int,
        "not_after_iso":     "2027-06-30T12:00:00+00:00",
        "fingerprint_sha256": "AB:CD:...",
        "subject_cn":        "opn-cockpit",
        "last_checked_iso":  "2026-07-19T12:00:00+00:00",
        "auto_restart_scheduled": bool,
    }
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock, Thread
from typing import Any

from opn_cockpit.audit.backend import AuditBackend
from opn_cockpit.audit.log import AuditEventKind

_log = logging.getLogger(__name__)

DEFAULT_TICK_S = 6 * 3600     # 6 Stunden
INITIAL_DELAY_S = 10
WARN_DAYS = 30
CRITICAL_DAYS = 7

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_CRITICAL = "critical"
STATUS_EXPIRED = "expired"


class TlsWatcher:
    """Hintergrund-Watcher fuer das aktive Server-Zertifikat.

    ``restart_callback`` ist eine 0-arg Callable, die den Prozess-
    Restart triggert (Windows/systemd). Wird bei abgelaufenem Auto-
    Cert aufgerufen nachdem ein frisches Cert auf Disk liegt. ``None``
    im Dev-Mode - dann bleibt der Server offline mit klarer Audit-
    Meldung.
    """

    def __init__(
        self,
        audit_backend: AuditBackend,
        *,
        tick_interval_s: int = DEFAULT_TICK_S,
        restart_callback=None,   # type: ignore[no-untyped-def]
    ) -> None:
        self._audit = audit_backend
        self._tick_interval_s = tick_interval_s
        self._restart_callback = restart_callback
        self._lock = RLock()
        self._thread: Thread | None = None
        self._stop = False
        self._state: dict[str, Any] = {
            "status": STATUS_OK,
            "cert_type": "none",
            "days_left": None,
            "not_after_iso": "",
            "fingerprint_sha256": "",
            "subject_cn": "",
            "last_checked_iso": "",
            "auto_restart_scheduled": False,
        }
        # Zuletzt beobachtete Kombination (status, fingerprint) - damit
        # Audit-Events nur bei Uebergaengen fliegen, nicht in jedem Tick.
        self._last_seen: tuple[str, str] | None = None

    # ----- Lifecycle -----

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = False
            self._thread = Thread(
                target=self._loop, daemon=True, name="opn-tls-watcher",
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop = True

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    # ----- Loop -----

    def _loop(self) -> None:
        time.sleep(INITIAL_DELAY_S)
        while True:
            if self._stop:
                return
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                _log.exception("tls-watcher tick failed")
            time.sleep(self._tick_interval_s)

    def _tick(self) -> None:
        """Prueft das aktive Cert und updated Zustand + Audit."""
        # Aktives Cert bestimmen: Custom hat Vorrang, sonst Auto.
        from opn_cockpit.config import AppSettings  # noqa: PLC0415
        from opn_cockpit.security.self_signed import (  # noqa: PLC0415
            AutoCertPaths,
            generate_self_signed,
        )
        try:
            from cryptography import x509                       # noqa: PLC0415
            from cryptography.hazmat.primitives import hashes   # noqa: PLC0415
        except ImportError:
            _log.warning("cryptography nicht verfuegbar - Watcher untaetig.")
            return

        app_settings = AppSettings.load()
        custom = app_settings.resolved_tls_paths()
        auto_paths = AutoCertPaths.from_dir(app_settings.auto_cert_directory())

        cert_type: str
        cert_path: Path
        if custom is not None:
            cert_type = "custom"
            cert_path = custom[0]
        elif auto_paths.cert.exists():
            cert_type = "auto"
            cert_path = auto_paths.cert
        else:
            # Kein Cert - HTTP-Fallback aktiv oder Boot hat versagt.
            with self._lock:
                self._state.update({
                    "status": STATUS_OK,
                    "cert_type": "none",
                    "days_left": None,
                    "not_after_iso": "",
                    "fingerprint_sha256": "",
                    "subject_cn": "",
                    "last_checked_iso": datetime.now(UTC).isoformat(),
                })
            return

        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        except (OSError, ValueError) as exc:
            _log.warning("Cert %s nicht lesbar: %s", cert_path, exc)
            with self._lock:
                self._state.update({
                    "status": STATUS_EXPIRED,
                    "cert_type": cert_type,
                    "days_left": -1,
                    "not_after_iso": "",
                    "fingerprint_sha256": "",
                    "subject_cn": "",
                    "last_checked_iso": datetime.now(UTC).isoformat(),
                })
            return

        not_after = _get_not_after(cert)
        days_left = (not_after - datetime.now(UTC)).days
        fingerprint_bytes = cert.fingerprint(hashes.SHA256())
        fingerprint_hex = fingerprint_bytes.hex().upper()
        fingerprint_colon = ":".join(
            fingerprint_hex[i : i + 2] for i in range(0, len(fingerprint_hex), 2)
        )
        subject_cn = _common_name(cert.subject)

        status = _classify(days_left)
        auto_restart_scheduled = False

        # Handling nach Cert-Typ + Status:
        if cert_type == "auto":
            if status == STATUS_WARN:
                # Regeneriere sofort - der neue Cert liegt bereit fuer den
                # naechsten Restart. Wenn wir das nicht tun, faelscht der
                # naechste Watcher-Tick "ok" auf frisches Cert und das
                # UI-Banner wuerde nie erscheinen.
                self._regenerate_auto(auto_paths, subject_cn_before=subject_cn)
            elif status == STATUS_EXPIRED:
                # Regen + Restart triggern
                new_gc = self._regenerate_auto(auto_paths, subject_cn_before=subject_cn)
                if new_gc is not None and self._restart_callback is not None:
                    try:
                        self._restart_callback()
                        auto_restart_scheduled = True
                    except Exception:  # noqa: BLE001
                        _log.exception("Auto-Restart-Trigger fehlgeschlagen")

        # State speichern
        with self._lock:
            self._state.update({
                "status": status,
                "cert_type": cert_type,
                "days_left": days_left,
                "not_after_iso": not_after.isoformat(),
                "fingerprint_sha256": fingerprint_colon,
                "subject_cn": subject_cn,
                "last_checked_iso": datetime.now(UTC).isoformat(),
                "auto_restart_scheduled": auto_restart_scheduled,
            })

        # Audit nur bei Uebergang.
        seen_key = (status, fingerprint_colon)
        if self._last_seen != seen_key:
            self._audit_transition(
                status=status,
                cert_type=cert_type,
                days_left=days_left,
                fingerprint=fingerprint_colon,
                subject_cn=subject_cn,
                not_after_iso=not_after.isoformat(),
                auto_restart_scheduled=auto_restart_scheduled,
            )
            self._last_seen = seen_key

    # ----- Helpers -----

    def _regenerate_auto(
        self,
        paths,  # type: ignore[no-untyped-def]
        *,
        subject_cn_before: str,
    ):  # type: ignore[no-untyped-def]
        from opn_cockpit.security.self_signed import generate_self_signed  # noqa: PLC0415
        try:
            new_gc = generate_self_signed(paths)
        except Exception as exc:  # noqa: BLE001
            _log.exception("Auto-Cert-Regeneration im Watcher fehlgeschlagen")
            self._audit.append(
                AuditEventKind.TLS_CERT_EXPIRY_CRITICAL,
                actor="system",
                action="tls_cert_regen_failed",
                error_kind="tls_regen",
                summary=(
                    f"Auto-Regeneration des TLS-Certs schlug fehl: {exc}. "
                    "Bitte manuell im Server-TLS-Modal 'Neu generieren' klicken."
                ),
            )
            return None
        self._audit.append(
            AuditEventKind.TLS_CERT_ROTATED,
            actor="system",
            action="tls_cert_rotated",
            summary=(
                f"TLS-Auto-Cert regeneriert. Neuer Fingerprint: "
                f"SHA-256:{new_gc.fingerprint_sha256}, gueltig bis "
                f"{new_gc.not_after_iso}. Ein Server-Restart aktiviert das neue Cert."
            ),
        )
        return new_gc

    def _audit_transition(
        self,
        *,
        status: str,
        cert_type: str,
        days_left: int,
        fingerprint: str,
        subject_cn: str,
        not_after_iso: str,
        auto_restart_scheduled: bool,
    ) -> None:
        if status == STATUS_WARN:
            self._audit.append(
                AuditEventKind.TLS_CERT_EXPIRY_WARNING,
                actor="system",
                action="tls_cert_expiry_warning",
                summary=(
                    f"TLS-Cert ({cert_type}, CN={subject_cn}) laeuft in "
                    f"{days_left} Tagen ab ({not_after_iso}). "
                    f"Fingerprint: SHA-256:{fingerprint}."
                ),
            )
        elif status == STATUS_CRITICAL:
            self._audit.append(
                AuditEventKind.TLS_CERT_EXPIRY_CRITICAL,
                actor="system",
                action="tls_cert_expiry_critical",
                error_kind="tls_expiry_critical",
                summary=(
                    f"TLS-Cert ({cert_type}, CN={subject_cn}) laeuft in "
                    f"{days_left} Tagen ab. Server-Restart im UI (\"Server "
                    "neu starten\") empfohlen."
                ),
            )
        elif status == STATUS_EXPIRED:
            self._audit.append(
                AuditEventKind.TLS_CERT_EXPIRY_CRITICAL,
                actor="system",
                action="tls_cert_expired",
                error_kind="tls_expired",
                summary=(
                    f"TLS-Cert ({cert_type}, CN={subject_cn}) ist ABGELAUFEN. "
                    + (
                        "Auto-Restart mit neuem Cert eingeleitet."
                        if auto_restart_scheduled
                        else "Bitte manuell neu starten oder frisches Cert hochladen."
                    )
                ),
            )


def _classify(days_left: int) -> str:
    if days_left < 0:
        return STATUS_EXPIRED
    if days_left <= CRITICAL_DAYS:
        return STATUS_CRITICAL
    if days_left <= WARN_DAYS:
        return STATUS_WARN
    return STATUS_OK


def _get_not_after(cert) -> datetime:  # type: ignore[no-untyped-def]
    raw = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    if raw.tzinfo is None:
        return raw.replace(tzinfo=UTC)
    return raw.astimezone(UTC)


def _common_name(name) -> str:  # type: ignore[no-untyped-def]
    from cryptography.x509.oid import NameOID  # noqa: PLC0415
    attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
    if attrs:
        return str(attrs[0].value)
    return ""


__all__ = [
    "CRITICAL_DAYS",
    "DEFAULT_TICK_S",
    "STATUS_CRITICAL",
    "STATUS_EXPIRED",
    "STATUS_OK",
    "STATUS_WARN",
    "TlsWatcher",
    "WARN_DAYS",
]
