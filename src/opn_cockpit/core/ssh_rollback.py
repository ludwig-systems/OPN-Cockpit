"""SSH-basierter Rollback auf einer OPNsense fuer das Safety-Net-Feature.

Wenn ein User "Apply mit Sicherheitsnetz" waehlt und die Box danach nicht
mehr erreichbar ist (z. B. weil eine Firewall-Regel die Cockpit-IP gesperrt
hat), faehrt der SafetyNetWatcher diese Routine an: SSH auf die Box, die
Pre-Apply-XML auf ``/conf/config.xml`` schreiben, ``configctl`` zum Reload
triggern. Die Box ist danach wieder im pre-Apply-Zustand.

Voraussetzungen:

* SSH-Zugang per Private-Key (Password-Auth bewusst nicht unterstuetzt -
  das waere ein zweites Secret-Profil im Tresor, das wir vermeiden).
* Der SSH-User braucht Schreibrechte auf ``/conf/config.xml`` und darf
  ``configctl`` ausfuehren. Bei OPNsense uebernimmt das ``root`` von
  Haus aus.
* paramiko ist Runtime-Dep (pyproject.toml).

Sicherheit:

* Kein known_hosts-Auto-Akzeptanz im Persistenz-Sinne - paramiko nutzt
  ``AutoAddPolicy`` weil der Cockpit-Operator die OPNsense schon ueber
  die API/TLS validiert hat; ein MitM-Szenario fuer Recovery-SSH ist
  kein realistisches Bedrohungsmodell.
* Klartext-Key wird sofort nach dem Connect ueber den ``finally``-Block
  in eine ``del``-Zeile geleitet damit er nicht in Tracebacks landet.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opn_cockpit.core.errors import (
    OpnCockpitError,
    UnreachableError,
    make_context,
)

if TYPE_CHECKING:
    from opn_cockpit.vault.model import VaultDevice

_log = logging.getLogger(__name__)

# OPNsense-Path-Konstanten - nicht User-konfigurierbar, das waere ein
# Footgun. Wer das ueberschreiben muss soll den Code direkt anpassen.
REMOTE_CONFIG_PATH = "/conf/config.xml"
REMOTE_BACKUP_BEFORE_RESTORE = "/conf/config.xml.opncockpit-before-restore"
RELOAD_COMMAND = (
    "configctl webgui restart renew; "
    "configctl filter reload; "
    "configctl interface reconfigure; "
    "configctl service reload all"
)


@dataclass(frozen=True, slots=True)
class SshRollbackResult:
    """Resultat eines Rollback-Versuchs - fuer Audit + UI."""

    success: bool
    summary: str
    pre_apply_backup_id: str = ""


def perform_ssh_rollback(
    device: VaultDevice,
    pre_apply_xml: bytes,
    *,
    pre_apply_backup_id: str = "",
    connect_timeout_s: float = 15.0,
    command_timeout_s: float = 60.0,
) -> SshRollbackResult:
    """Pusht ``pre_apply_xml`` per SSH auf das Geraet und triggert Reload.

    Defensive Fehlerbehandlung: jede Stoerung wird zu einem strukturierten
    Result statt einer Exception, damit der Watcher den Fehler in den
    Audit-Log schreiben kann ohne ein Crash-Risiko.
    """
    # Spaeter Import - paramiko ist groesser, sollte erst gezogen werden
    # wenn das Feature tatsaechlich getriggert wird.
    try:
        import paramiko  # noqa: PLC0415
    except ImportError as exc:
        return SshRollbackResult(
            success=False,
            summary=f"paramiko nicht installiert: {exc}",
            pre_apply_backup_id=pre_apply_backup_id,
        )

    if not device.ssh_enabled:
        return SshRollbackResult(
            success=False,
            summary="SSH-Safety-Net auf diesem Geraet nicht aktiviert.",
            pre_apply_backup_id=pre_apply_backup_id,
        )
    if not device.ssh_private_key_pem.strip():
        return SshRollbackResult(
            success=False,
            summary="Kein SSH-Private-Key im Tresor hinterlegt.",
            pre_apply_backup_id=pre_apply_backup_id,
        )
    if not device.ssh_user.strip():
        return SshRollbackResult(
            success=False,
            summary="Kein SSH-User im Tresor hinterlegt.",
            pre_apply_backup_id=pre_apply_backup_id,
        )

    host = device.ssh_host.strip() or device.host
    port = device.ssh_port or 22

    private_key = None
    client = None
    try:
        # Mehrere Key-Formate akzeptieren - User koennen RSA, Ed25519,
        # ECDSA hinterlegen.
        key_io = io.StringIO(device.ssh_private_key_pem)
        for key_cls in (
            paramiko.Ed25519Key,
            paramiko.ECDSAKey,
            paramiko.RSAKey,
            paramiko.DSSKey,
        ):
            try:
                key_io.seek(0)
                private_key = key_cls.from_private_key(key_io)
                break
            except paramiko.SSHException:
                continue
        if private_key is None:
            return SshRollbackResult(
                success=False,
                summary=(
                    "SSH-Private-Key nicht lesbar - akzeptierte Formate: "
                    "Ed25519, ECDSA, RSA, DSA (PEM)."
                ),
                pre_apply_backup_id=pre_apply_backup_id,
            )

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            port=port,
            username=device.ssh_user,
            pkey=private_key,
            timeout=connect_timeout_s,
            allow_agent=False,
            look_for_keys=False,
        )

        # Schritt 1: alten Stand sichern (Forensik) - bewusst BEVOR wir
        # die neue Datei pushen, damit der Operator sehen kann was kaputt
        # war.
        backup_cmd = (
            f"cp {REMOTE_CONFIG_PATH} {REMOTE_BACKUP_BEFORE_RESTORE}"
        )
        _exec(client, backup_cmd, command_timeout_s)

        # Schritt 2: Pre-Apply-XML schreiben via SFTP.
        sftp = client.open_sftp()
        try:
            with sftp.file(REMOTE_CONFIG_PATH, "wb") as f:
                f.write(pre_apply_xml)
            sftp.chmod(REMOTE_CONFIG_PATH, 0o600)
        finally:
            sftp.close()

        # Schritt 3: Reload trigger. Wenn das fehlschlaegt, ist die
        # Config zwar zurueck, aber noch nicht aktiviert - der Operator
        # muss dann manuell reload. Loggen wir auch entsprechend.
        stdout, stderr = _exec(client, RELOAD_COMMAND, command_timeout_s)

        return SshRollbackResult(
            success=True,
            summary=(
                f"SSH-Rollback ok auf {host}:{port}. "
                f"Pre-Apply-Backup wiederhergestellt; reload-cmd ok."
            ),
            pre_apply_backup_id=pre_apply_backup_id,
        )
    except paramiko.AuthenticationException as exc:
        return SshRollbackResult(
            success=False,
            summary=f"SSH-Authentifizierung fehlgeschlagen ({host}): {exc}",
            pre_apply_backup_id=pre_apply_backup_id,
        )
    except paramiko.SSHException as exc:
        return SshRollbackResult(
            success=False,
            summary=f"SSH-Fehler ({host}): {exc}",
            pre_apply_backup_id=pre_apply_backup_id,
        )
    except OSError as exc:
        return SshRollbackResult(
            success=False,
            summary=f"SSH-Verbindung nicht moeglich ({host}:{port}): {exc}",
            pre_apply_backup_id=pre_apply_backup_id,
        )
    except Exception as exc:  # noqa: BLE001
        _log.exception("Unerwarteter Fehler im SSH-Rollback auf %s", host)
        return SshRollbackResult(
            success=False,
            summary=f"Unerwarteter Fehler: {type(exc).__name__}",
            pre_apply_backup_id=pre_apply_backup_id,
        )
    finally:
        # Klartext-Key + Connection-Handle aufraeumen - kein
        # persistentes Halten von Auth-Material.
        try:
            if client is not None:
                client.close()
        except Exception:  # noqa: BLE001, S110
            pass
        del private_key


def _exec(client, command: str, timeout_s: float) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    """Fuehrt ``command`` aus und liefert (stdout, stderr) als Text.

    Wirft ``UnreachableError`` wenn der Exit-Code != 0 - Aufrufer faengt
    das wie jeden anderen Cockpit-Fehler.
    """
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout_s)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        raise UnreachableError(
            f"Remote-Befehl fehlgeschlagen (rc={rc}): {command}",
            context=make_context(
                error_kind="ssh_command_failed",
                summary=err.strip() or out.strip() or f"rc={rc}",
            ),
        )
    return out, err


__all__ = [
    "SshRollbackResult",
    "perform_ssh_rollback",
]
