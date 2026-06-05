"""Endpoint fuer das Server-eigene HTTPS-Zertifikat.

Im Gegensatz zum Custom-Trust-Store (der OUTGOING TLS-Pruefung gegen
OPNsense-Boxen beeinflusst), regelt dieses Endpoint das INGOING TLS:
das Zertifikat unter dem Cockpit-User auf ``https://cockpit.lab:9876``
zugreifen.

Pfade liegen in ``%APPDATA%/OPN-Cockpit/settings.json`` (AppSettings) -
das ist bewusst ausserhalb des Tresors, weil der Server vor jedem
Vault-Unlock hochkommen muss. Die Cert/Key-Dateien selbst landen unter
``<app_data>/server_tls/`` mit 0600.

Nach jedem POST/DELETE muss der User den Cockpit-Prozess neu starten -
uvicorn liest TLS nur beim Boot. Der Endpoint liefert deshalb in der
Antwort einen ``requires_restart``-Flag.
"""

from __future__ import annotations

import os
import ssl
import stat
import tempfile

from fastapi import APIRouter, Depends, HTTPException, status

from opn_cockpit.audit.backend import audit_actor, get_audit_backend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.config import AppSettings, get_app_data_dir, get_settings_path
from opn_cockpit.core.trust_store import parse_cert
from opn_cockpit.security.session import Session
from opn_cockpit.web.api.schemas import (
    ServerTlsStatusResponse,
    ServerTlsUploadRequest,
)
from opn_cockpit.web.acl import require_admin_role
from opn_cockpit.web.auth.dependencies import require_session

router = APIRouter(prefix="/api/server", tags=["server-tls"])

_TLS_SUBDIR = "server_tls"
_CERT_FILENAME = "cert.pem"
_KEY_FILENAME = "key.pem"


@router.get("/tls", response_model=ServerTlsStatusResponse)
def get_server_tls(
    session: Session = Depends(require_session),
) -> ServerTlsStatusResponse:
    """Liefert den aktuellen Server-TLS-Status.

    Admin-only - das ist eine app-weite Setting, nicht pro Tresor. Im
    Single-User-Mode ist der eingeloggte User implizit admin.
    """
    session.touch()
    app_settings = AppSettings.load()
    cert_path = app_settings.server_tls_cert_path
    key_path = app_settings.server_tls_key_path
    resolved = app_settings.resolved_tls_paths()
    response = ServerTlsStatusResponse(
        enabled=resolved is not None,
        cert_path=cert_path or "",
        key_path=key_path or "",
        cert_subject_cn="",
        cert_not_after_iso="",
        cert_days_until_expiry=None,
        warnings=[],
    )
    if resolved is None:
        if cert_path or key_path:
            response.warnings.append(
                "Eingetragene Pfade existieren nicht oder sind unvollstaendig. "
                "Cockpit faehrt deshalb mit HTTP.",
            )
        return response
    cert_file, _key_file = resolved
    try:
        meta = parse_cert(cert_file.read_text(encoding="ascii"))
    except (OSError, ValueError) as exc:
        response.warnings.append(f"Cert nicht lesbar/parsbar: {exc}")
        return response
    response.cert_subject_cn = meta.subject_cn
    response.cert_not_after_iso = meta.not_after_iso
    response.cert_days_until_expiry = meta.days_until_expiry
    if meta.days_until_expiry is not None and meta.days_until_expiry < 0:
        response.warnings.append("Server-Zertifikat ist ABGELAUFEN!")
    elif meta.days_until_expiry is not None and meta.days_until_expiry < 14:
        response.warnings.append(
            f"Server-Zertifikat laeuft in {meta.days_until_expiry} Tagen ab.",
        )
    return response


@router.post("/tls", response_model=ServerTlsStatusResponse)
def upload_server_tls(
    payload: ServerTlsUploadRequest,
    session: Session = Depends(require_session),
) -> ServerTlsStatusResponse:
    """Schreibt Cert + Key in den App-Daten-Ordner und persistiert die
    Pfade in der ``settings.json``.

    Validierung:

    * Cert ist ein parsbares X.509 (sonst 422).
    * Key wird inhaltlich NICHT geparst (zu viele Formate, paramiko/
      cryptography haben hier verschiedene Erwartungen) - aber die Datei
      muss ``-----BEGIN``-Header enthalten und Permissions werden auf
      0600 gesetzt.
    * Nach Save: ``requires_restart=True``.
    """
    require_admin_role(session)
    session.touch()
    cert_pem = (payload.cert_pem or "").strip()
    key_pem = (payload.key_pem or "").strip()
    if "BEGIN CERTIFICATE" not in cert_pem:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="cert_pem enthaelt keinen '-----BEGIN CERTIFICATE-----'-Block.",
        )
    if "BEGIN" not in key_pem or "PRIVATE KEY" not in key_pem:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "key_pem sieht nicht wie ein PEM-Private-Key aus "
                "(erwartet einen '-----BEGIN ... PRIVATE KEY-----'-Block)."
            ),
        )
    try:
        meta = parse_cert(cert_pem)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"cert_pem nicht parsbar: {exc}",
        ) from exc

    # Audit-Finding F2: Cert + Key probeladen, bevor wir auf die echte
    # Konfig schreiben. Schlaegt der Match fehl, scheitert uvicorn beim
    # naechsten Boot mit SSL-Error - das machen wir lieber hier sichtbar
    # statt den Service zu briken.
    try:
        _verify_cert_and_key(cert_pem, key_pem)
    except ssl.SSLError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Cert und Key passen nicht zusammen oder Key ist nicht "
                f"lesbar: {exc}"
            ),
        ) from exc
    except OSError as exc:
        # Sollte praktisch nicht passieren - temp-File-Probleme
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"TLS-Probelauf fehlgeschlagen: {exc}",
        ) from exc

    tls_dir = get_app_data_dir() / _TLS_SUBDIR
    tls_dir.mkdir(parents=True, exist_ok=True)
    cert_file = tls_dir / _CERT_FILENAME
    key_file = tls_dir / _KEY_FILENAME
    cert_file.write_text(cert_pem if cert_pem.endswith("\n") else cert_pem + "\n",
                         encoding="ascii")
    key_file.write_text(key_pem if key_pem.endswith("\n") else key_pem + "\n",
                        encoding="ascii")
    # 0600 fuer den Key; 0644 fuer das Cert (Public). Auf Windows ist
    # chmod weitgehend ein No-Op, schadet aber nicht.
    try:
        os.chmod(cert_file, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    app_settings = AppSettings.load()
    app_settings.server_tls_cert_path = str(cert_file)
    app_settings.server_tls_key_path = str(key_file)
    app_settings.save(get_settings_path())

    get_audit_backend().append(
        AuditEventKind.VAULT_OPENED,
        actor=audit_actor(session),
        summary=(
            f"Server-TLS-Zertifikat gesetzt (Subject={meta.subject_cn}, "
            f"fp={meta.fingerprint_sha256[:32]}, "
            f"gueltig bis {meta.not_after_iso[:10]}). "
            "Restart erforderlich."
        ),
    )

    return ServerTlsStatusResponse(
        enabled=True,
        cert_path=str(cert_file),
        key_path=str(key_file),
        cert_subject_cn=meta.subject_cn,
        cert_not_after_iso=meta.not_after_iso,
        cert_days_until_expiry=meta.days_until_expiry,
        requires_restart=True,
        warnings=[],
    )


@router.delete("/tls", status_code=status.HTTP_204_NO_CONTENT)
def disable_server_tls(
    session: Session = Depends(require_session),
) -> None:
    require_admin_role(session)
    """Entfernt die Server-TLS-Konfiguration aus settings.json.

    Die Dateien unter ``<app_data>/server_tls/`` werden NICHT geloescht
    (Audit-Trail / Recover) - nur die Pfade aus settings.json. Nach
    Restart laeuft Cockpit wieder auf HTTP.
    """
    session.touch()
    app_settings = AppSettings.load()
    had_cert = bool(app_settings.server_tls_cert_path)
    app_settings.server_tls_cert_path = None
    app_settings.server_tls_key_path = None
    app_settings.save(get_settings_path())
    if had_cert:
        get_audit_backend().append(
            AuditEventKind.VAULT_OPENED,
            actor=audit_actor(session),
            summary=(
                "Server-TLS-Konfiguration entfernt - Cockpit faehrt nach "
                "Restart wieder auf HTTP."
            ),
        )


def _verify_cert_and_key(cert_pem: str, key_pem: str) -> None:
    """Probe-Lauf: laedt Cert + Key in einen SSLContext.

    Wirft ``ssl.SSLError`` wenn Cert+Key nicht zueinander passen oder
    der Key nicht parsbar ist. Die temp-Files werden mit 0600 angelegt
    und sofort wieder geloescht.
    """
    cert_fd, cert_path = tempfile.mkstemp(
        prefix="opncockpit-tls-probe-cert-", suffix=".pem",
    )
    key_fd, key_path = tempfile.mkstemp(
        prefix="opncockpit-tls-probe-key-", suffix=".pem",
    )
    try:
        with os.fdopen(cert_fd, "w", encoding="ascii") as f:
            f.write(cert_pem if cert_pem.endswith("\n") else cert_pem + "\n")
        with os.fdopen(key_fd, "w", encoding="ascii") as f:
            f.write(key_pem if key_pem.endswith("\n") else key_pem + "\n")
        try:
            os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        ctx = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
        # load_cert_chain wirft SSLError wenn Key nicht zum Cert passt
        # oder Key-Format unbekannt ist.
        ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    finally:
        for path in (cert_path, key_path):
            try:
                os.unlink(path)
            except OSError:
                pass


__all__ = ["router"]
