"""On-Device Dead-Man's-Switch fuer das Safety-Net-Feature.

Das v0.8-Konzept (Cockpit-seitiger Watcher + nachgelagerter SSH-Rollback)
hatte eine Luecke: wenn der Apply Cockpit aus der Box aussperrt, kann
auch der Rollback-SSH-Pfad nicht mehr aufgebaut werden. Diese v0.9-
Loesung verschiebt den Timer auf die OPNsense selbst.

Ablauf pro Apply mit Safety-Net auf einem Geraet:

1. **ARM** (Cockpit -> FW): SFTP die Pre-Apply-XML nach
   ``/conf/config.xml.cockpit-safety-<jobid>.xml``; starte ueber
   ``daemon(8)`` einen detached Sleep-Job mit PID-File, der nach
   ``window_s`` Sekunden einen Marker schreibt, die Safety-XML
   zurueckschiebt und die Box rebootet. Verifikation: PID-File lesen,
   ``kill -0`` checken.
2. **APPLY** (Cockpit -> FW): die API-Aktionen wie gehabt.
3. **DISARM** (Cockpit -> FW): kill den PID, raeume Safety-XML und
   PID-File auf. Wenn Cockpit hier nicht mehr ran kommt (genau das ist
   der Lockout-Fall), feuert der Daemon irgendwann von selbst, restored
   die Pre-Apply-XML und rebootet. Beim naechsten Reconnect kann der
   Cockpit-Watcher den Marker erkennen und der UI ein klares
   "Dead-Man hat ausgeloest"-Banner anzeigen.
4. **TEST-LOOP** (Cockpit -> FW): identischer Ablauf, aber mit
   ``dry_marker_only=True``: der Daemon macht statt restore+reboot nur
   ``touch marker``, damit der User die Mechanik gefahrlos auf einer
   Produktiv-Box validieren kann.

Sicherheits-Annahmen:

* SSH-User braucht root-Rechte (Schreiben in ``/conf/``, ``daemon(8)``,
  ``shutdown(8)``). Auf OPNsense ist das ``root`` von Haus aus.
* Authentifizierung ausschliesslich per Private-Key. Der Key liegt
  verschluesselt im Tresor (gleiches Schutzniveau wie ``api_secret``).
* paramiko nutzt ``AutoAddPolicy``: das MitM-Szenario fuer Recovery-SSH
  zwischen Cockpit und einer bereits via API/TLS validierten OPNsense
  ist kein realistisches Bedrohungsmodell.
"""

from __future__ import annotations

import io
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opn_cockpit.vault.model import VaultDevice

_log = logging.getLogger(__name__)

# Datei-Pfade auf der Box - Konstanten weil "konfigurierbar" hier ein
# Footgun waere. Wer das ueberschreiben muss, soll den Code anpassen.
REMOTE_SAFETY_XML_TEMPLATE = "/conf/config.xml.cockpit-safety-{jobid}.xml"
REMOTE_PID_FILE_TEMPLATE = "/var/run/cockpit-safety-{jobid}.pid"
REMOTE_FIRE_MARKER_TEMPLATE = "/var/log/cockpit-safety-{jobid}.fired"
REMOTE_CONFIG_PATH = "/conf/config.xml"

# Jobid darf nur alphanumerisch / Bindestrich / Unterstrich enthalten -
# alles andere koennte Shell-Injection im daemon-Befehl ermoeglichen.
_JOBID_RE = re.compile(r"^[A-Za-z0-9_-]{3,80}$")


@dataclass(frozen=True, slots=True)
class ArmResult:
    """Resultat eines Arm-Versuchs."""

    success: bool
    summary: str
    pid: int = 0  # PID des laufenden daemon-Jobs auf der Box (0 wenn nicht ermittelbar)


@dataclass(frozen=True, slots=True)
class DisarmResult:
    """Resultat eines Disarm-Versuchs."""

    success: bool
    summary: str


@dataclass(frozen=True, slots=True)
class MarkerResult:
    """Resultat eines Marker-Checks."""

    success: bool          # SSH war erreichbar; das sagt nichts ueber den Marker
    fired: bool            # True = Marker gefunden = Daemon hat ausgeloest
    summary: str


@dataclass(frozen=True, slots=True)
class TestResult:
    """Resultat des End-to-End-Test-Loops im Host-Modal."""

    __test__ = False  # Pytest: keine Test-Klasse, sondern Dataclass-Result

    success: bool
    summary: str
    failed_step: str = ""  # leer wenn success, sonst arm|verify|wait|marker|cleanup


def make_jobid(plan_id: str, device_id: str) -> str:
    """Baut eine deterministische, shell-sichere Job-ID aus plan + device.

    Format: ``<plan_id_first_segment>-<device_id_first_8>``. Wenn der
    plan_id keine sicheren Zeichen liefert, faellt auf ``apply`` zurueck.
    """
    plan_safe = re.sub(r"[^A-Za-z0-9_-]", "", plan_id)[:24] or "apply"
    dev_safe = re.sub(r"[^A-Za-z0-9_-]", "", device_id)[:8] or "dev"
    return f"{plan_safe}-{dev_safe}"


def _validate_jobid(jobid: str) -> None:
    if not _JOBID_RE.match(jobid):
        raise ValueError(
            f"Ungueltige jobid '{jobid}' - nur alphanumerisch, '-' und '_' erlaubt.",
        )


def _paths(jobid: str) -> tuple[str, str, str]:
    """Liefert (safety_xml, pid_file, marker)."""
    _validate_jobid(jobid)
    return (
        REMOTE_SAFETY_XML_TEMPLATE.format(jobid=jobid),
        REMOTE_PID_FILE_TEMPLATE.format(jobid=jobid),
        REMOTE_FIRE_MARKER_TEMPLATE.format(jobid=jobid),
    )


# ---------------------------------------------------------------------------
# SSH-Connect-Helper
# ---------------------------------------------------------------------------


def _connect(
    device: VaultDevice,
    *,
    connect_timeout_s: float,
):  # type: ignore[no-untyped-def]
    """Baut eine SSH-Verbindung auf. Liefert (client, None) bei Erfolg,
    sonst (None, summary).

    Wir liefern *keine* Exception zurueck, weil alle Aufrufer Result-
    Objekte produzieren wollen - Exception waere hier nur Stoerfeuer.
    """
    try:
        import paramiko  # noqa: PLC0415
    except ImportError as exc:
        return None, f"paramiko nicht installiert: {exc}"

    if not device.ssh_enabled:
        return None, "SSH-Safety-Net auf diesem Geraet nicht aktiviert."
    if not device.ssh_private_key_pem.strip():
        return None, "Kein SSH-Private-Key im Tresor hinterlegt."
    if not device.ssh_user.strip():
        return None, "Kein SSH-User im Tresor hinterlegt."

    host = device.ssh_host.strip() or device.host
    port = device.ssh_port or 22

    key_io = io.StringIO(device.ssh_private_key_pem)
    private_key = None
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
        return None, (
            "SSH-Private-Key nicht lesbar - akzeptierte Formate: "
            "Ed25519, ECDSA, RSA, DSA (PEM)."
        )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=device.ssh_user,
            pkey=private_key,
            timeout=connect_timeout_s,
            allow_agent=False,
            look_for_keys=False,
        )
    except paramiko.AuthenticationException as exc:
        return None, f"SSH-Authentifizierung fehlgeschlagen ({host}): {exc}"
    except paramiko.SSHException as exc:
        return None, f"SSH-Fehler ({host}): {exc}"
    except OSError as exc:
        return None, f"SSH-Verbindung nicht moeglich ({host}:{port}): {exc}"
    finally:
        del private_key
    return client, None


def _exec(client, command: str, timeout_s: float) -> tuple[int, str, str]:  # type: ignore[no-untyped-def]
    """Fuehrt ``command`` aus und liefert (rc, stdout, stderr) als Text.

    Wirft keine Exception bei rc != 0 - der Aufrufer entscheidet was ein
    Fehler ist.
    """
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout_s)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


# ---------------------------------------------------------------------------
# Daemon-Befehle
# ---------------------------------------------------------------------------


def _build_daemon_cmd(
    *,
    jobid: str,
    window_s: int,
    dry_marker_only: bool,
) -> str:
    """Baut den ``daemon(8)``-Aufruf, der den Dead-Man's-Switch arm't.

    Produktiv (dry_marker_only=False):
        daemon -f -p <pid> sh -c 'sleep N;
            touch <marker>;
            cp <safety-xml> /conf/config.xml;
            /sbin/shutdown -r now'

    Testmodus (dry_marker_only=True):
        daemon -f -p <pid> sh -c 'sleep N; touch <marker>'
    """
    safety_xml, pid_file, marker = _paths(jobid)
    if dry_marker_only:
        inner = f"sleep {int(window_s)}; touch {marker}"
    else:
        # Marker VOR restore + reboot - damit nach dem Reboot eine Spur
        # bleibt, dass der Dead-Man gefeuert hat.
        inner = (
            f"sleep {int(window_s)}; "
            f"touch {marker}; "
            f"cp {safety_xml} {REMOTE_CONFIG_PATH}; "
            f"/sbin/shutdown -r now"
        )
    # Single-Quotes um die Command-Liste; im inner sind nur normale
    # Whitespace + Pfade ohne Quotes, also kein Escape-Problem.
    return f"daemon -f -p {pid_file} sh -c '{inner}'"


def _build_verify_cmd(jobid: str) -> str:
    """Liest PID-File und prueft via ``kill -0`` ob der Prozess lebt."""
    _, pid_file, _ = _paths(jobid)
    return (
        f"if [ ! -f {pid_file} ]; then echo pid=missing; exit 0; fi; "
        f"PID=`cat {pid_file}`; "
        f"if kill -0 $PID 2>/dev/null; then echo pid=$PID; else echo pid=dead; fi"
    )


def _build_disarm_cmd(jobid: str) -> str:
    """Killt den daemon und raeumt Safety-Files + PID-File auf.

    Idempotent: doppeltes Ausfuehren ist ok. Wenn das PID-File fehlt
    (z. B. weil die Box schon rebootet hat) wird die ``kill``-Zeile
    leise uebersprungen.
    """
    safety_xml, pid_file, _ = _paths(jobid)
    return (
        f"if [ -f {pid_file} ]; then "
        f"kill -TERM `cat {pid_file}` 2>/dev/null || true; "
        f"fi; "
        f"rm -f {pid_file} {safety_xml}; "
        f"echo disarm-done"
    )


def _build_marker_check_cmd(jobid: str) -> str:
    _, _, marker = _paths(jobid)
    return f"if [ -f {marker} ]; then echo fired; else echo clean; fi"


def _build_marker_cleanup_cmd(jobid: str) -> str:
    """Raeumt nach erkanntem Fire alle Spuren weg."""
    safety_xml, pid_file, marker = _paths(jobid)
    return f"rm -f {marker} {safety_xml} {pid_file}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def arm(
    device: VaultDevice,
    *,
    jobid: str,
    pre_apply_xml: bytes,
    window_s: int,
    dry_marker_only: bool = False,
    connect_timeout_s: float = 15.0,
    command_timeout_s: float = 30.0,
) -> ArmResult:
    """Schiebt die Pre-Apply-XML auf die Box und startet den Dead-Man-Timer.

    Liefert ein strukturiertes ``ArmResult`` - keine Exceptions ausser
    bei Programmierfehlern. Wenn der Aufruf ``success=False`` liefert,
    war keine Aenderung auf der Box - der Aufrufer kann den Apply
    sicher abbrechen.
    """
    try:
        _validate_jobid(jobid)
    except ValueError as exc:
        return ArmResult(success=False, summary=str(exc))

    safety_xml_path, pid_file_path, _ = _paths(jobid)

    client, err = _connect(device, connect_timeout_s=connect_timeout_s)
    if client is None:
        return ArmResult(success=False, summary=err or "SSH-Connect fehlgeschlagen.")

    try:
        # Aufraeumen, falls von einem fruehen abgebrochenen Apply noch
        # Reste rumliegen (gleiche jobid wuerde sonst kollidieren).
        _exec(client, _build_disarm_cmd(jobid), command_timeout_s)

        # 1) SFTP push der Pre-Apply-XML
        try:
            sftp = client.open_sftp()
        except Exception as exc:  # noqa: BLE001
            return ArmResult(
                success=False,
                summary=f"SFTP-Channel nicht oeffenbar: {exc}",
            )
        try:
            try:
                with sftp.file(safety_xml_path, "wb") as f:
                    f.write(pre_apply_xml)
                sftp.chmod(safety_xml_path, 0o600)
            except Exception as exc:  # noqa: BLE001
                return ArmResult(
                    success=False,
                    summary=f"Safety-XML schreiben fehlgeschlagen: {exc}",
                )
        finally:
            try:
                sftp.close()
            except Exception:  # noqa: BLE001, S110
                pass

        # 2) Daemon starten
        daemon_cmd = _build_daemon_cmd(
            jobid=jobid, window_s=window_s, dry_marker_only=dry_marker_only,
        )
        rc, out, daemon_err = _exec(client, daemon_cmd, command_timeout_s)
        if rc != 0:
            # Bei Fehler: Safety-XML wieder loeschen, damit kein
            # halber Zustand auf der Box bleibt.
            _exec(client, f"rm -f {safety_xml_path}", command_timeout_s)
            return ArmResult(
                success=False,
                summary=(
                    f"daemon(8) konnte nicht gestartet werden (rc={rc}): "
                    f"{(daemon_err or out).strip()[:200]}"
                ),
            )

        # 3) PID verifizieren
        rc_v, verify_out, verify_err = _exec(
            client, _build_verify_cmd(jobid), command_timeout_s,
        )
        verify_out = verify_out.strip()
        if rc_v != 0 or not verify_out.startswith("pid="):
            _exec(client, _build_disarm_cmd(jobid), command_timeout_s)
            return ArmResult(
                success=False,
                summary=(
                    f"PID-Verify fehlgeschlagen (rc={rc_v}, out='{verify_out[:80]}', "
                    f"err='{verify_err.strip()[:120]}')"
                ),
            )
        pid_str = verify_out.split("=", 1)[1].strip()
        if pid_str in {"missing", "dead"}:
            _exec(client, _build_disarm_cmd(jobid), command_timeout_s)
            return ArmResult(
                success=False,
                summary=(
                    f"daemon(8) gestartet, aber Prozess sofort wieder weg ({pid_str}). "
                    "Pruef ob 'daemon' auf der Box vorhanden ist und der SSH-User "
                    "Schreibrechte in /var/run hat."
                ),
            )
        try:
            pid = int(pid_str)
        except ValueError:
            pid = 0

        return ArmResult(
            success=True,
            summary=(
                f"Safety-Net armed (jobid={jobid}, pid={pid}, window={window_s}s, "
                f"mode={'test' if dry_marker_only else 'live'})."
            ),
            pid=pid,
        )
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001, S110
            pass


def disarm(
    device: VaultDevice,
    *,
    jobid: str,
    connect_timeout_s: float = 10.0,
    command_timeout_s: float = 15.0,
) -> DisarmResult:
    """Killt den daemon, raeumt alle Spuren auf. Idempotent."""
    try:
        _validate_jobid(jobid)
    except ValueError as exc:
        return DisarmResult(success=False, summary=str(exc))

    client, err = _connect(device, connect_timeout_s=connect_timeout_s)
    if client is None:
        return DisarmResult(success=False, summary=err or "SSH-Connect fehlgeschlagen.")
    try:
        rc, out, err_out = _exec(client, _build_disarm_cmd(jobid), command_timeout_s)
        if rc != 0:
            return DisarmResult(
                success=False,
                summary=(
                    f"Disarm-Kommando rc={rc}: "
                    f"{(err_out or out).strip()[:160]}"
                ),
            )
        return DisarmResult(
            success=True,
            summary=f"Safety-Net disarmed (jobid={jobid}).",
        )
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001, S110
            pass


def check_marker(
    device: VaultDevice,
    *,
    jobid: str,
    cleanup_if_found: bool = True,
    connect_timeout_s: float = 10.0,
    command_timeout_s: float = 15.0,
) -> MarkerResult:
    """Prueft auf der Box, ob der Fire-Marker existiert (= Daemon hat ausgeloest).

    Wenn ``cleanup_if_found`` (Default): bei gefundenem Marker werden
    Marker + Safety-XML + PID-File geloescht, damit beim naechsten Apply
    nichts altes herumliegt.

    ``success=False`` heisst nur "SSH war nicht erreichbar"; ein
    erfolgreicher Check mit ``fired=False`` heisst "Box online, kein
    Marker da".
    """
    try:
        _validate_jobid(jobid)
    except ValueError as exc:
        return MarkerResult(success=False, fired=False, summary=str(exc))

    client, err = _connect(device, connect_timeout_s=connect_timeout_s)
    if client is None:
        return MarkerResult(
            success=False, fired=False, summary=err or "SSH-Connect fehlgeschlagen.",
        )
    try:
        rc, out, err_out = _exec(
            client, _build_marker_check_cmd(jobid), command_timeout_s,
        )
        out_clean = out.strip()
        if rc != 0:
            return MarkerResult(
                success=False, fired=False,
                summary=f"Marker-Check rc={rc}: {(err_out or out).strip()[:160]}",
            )
        fired = out_clean == "fired"
        if fired and cleanup_if_found:
            _exec(client, _build_marker_cleanup_cmd(jobid), command_timeout_s)
        return MarkerResult(
            success=True,
            fired=fired,
            summary="Marker gefunden." if fired else "Kein Marker - alles sauber.",
        )
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001, S110
            pass


def run_test_loop(
    device: VaultDevice,
    *,
    window_s: int = 20,
    extra_wait_s: int = 5,
    connect_timeout_s: float = 15.0,
    command_timeout_s: float = 30.0,
) -> TestResult:
    """End-to-End-Test: arm -> warten -> Marker pruefen -> aufraeumen.

    Setzt ``dry_marker_only=True`` - die Box bekommt **keinen Reboot
    und kein Restore**. Wenn alle Schritte gruen sind, weiss der User
    dass auf der Box ``daemon(8)`` da ist, die SSH-Permissions reichen
    und die Mechanik im Notfall funktionieren wuerde.

    Jobid wird hart auf ``cockpit-test`` gesetzt - kein paralleler
    Test pro Geraet, dafuer sehr klar in /var/log auffindbar.
    """
    jobid = "cockpit-test"
    dummy_xml = b"<?xml version=\"1.0\"?>\n<opncockpit_safety_net_test/>\n"

    # ARM
    arm_res = arm(
        device,
        jobid=jobid,
        pre_apply_xml=dummy_xml,
        window_s=window_s,
        dry_marker_only=True,
        connect_timeout_s=connect_timeout_s,
        command_timeout_s=command_timeout_s,
    )
    if not arm_res.success:
        return TestResult(
            success=False,
            summary=f"Arm fehlgeschlagen: {arm_res.summary}",
            failed_step="arm",
        )

    # WAIT (window + ein paar Sekunden Reserve damit der Daemon sicher gefeuert hat)
    time.sleep(max(1, window_s) + max(1, extra_wait_s))

    # MARKER CHECK
    marker_res = check_marker(
        device,
        jobid=jobid,
        cleanup_if_found=True,
        connect_timeout_s=connect_timeout_s,
        command_timeout_s=command_timeout_s,
    )
    if not marker_res.success:
        return TestResult(
            success=False,
            summary=f"Marker-Check ssh-seitig fehlgeschlagen: {marker_res.summary}",
            failed_step="marker",
        )
    if not marker_res.fired:
        # Cleanup trotzdem versuchen, damit nichts liegen bleibt
        disarm(device, jobid=jobid)
        return TestResult(
            success=False,
            summary=(
                "Daemon hat den Marker nicht gesetzt - pruef ob 'daemon(8)' und "
                "Schreibrechte auf /var/log existieren."
            ),
            failed_step="wait",
        )

    return TestResult(
        success=True,
        summary=(
            f"Test ok: arm -> {window_s}s sleep -> Marker erkannt -> aufgeraeumt. "
            "Der Dead-Man's-Switch wuerde auf dieser Box im Ernstfall arbeiten."
        ),
    )


__all__ = [
    "ArmResult",
    "DisarmResult",
    "MarkerResult",
    "TestResult",
    "arm",
    "check_marker",
    "disarm",
    "make_jobid",
    "run_test_loop",
]
