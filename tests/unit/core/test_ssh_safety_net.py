"""Tests fuer core.ssh_safety_net - On-Device Dead-Man's-Switch.

Wir mocken ``paramiko`` komplett via ``monkeypatch`` und einer
Fake-Klasse, die die SSHClient-Schnittstelle nachbildet. Damit testen
wir die Kommando-Konstruktion (daemon-Aufruf, Pfade), die Idempotenz
des Disarm-Pfads und das defensive Fehler-Mapping (kein Exception-Leak
ueber die Public-API).

Keine echten Sockets - die Tests rennen schnell.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field

import pytest

from opn_cockpit.core import ssh_safety_net
from opn_cockpit.core.ssh_safety_net import (
    ArmResult,
    DisarmResult,
    MarkerResult,
    TestResult,
    _build_daemon_cmd,
    _build_disarm_cmd,
    _build_marker_check_cmd,
    _build_verify_cmd,
    _validate_jobid,
    make_jobid,
)
from opn_cockpit.vault.model import VaultDevice

# ---------------------------------------------------------------------------
# Fake-Paramiko: minimaler Stub, der die echten APIs abbildet.
# ---------------------------------------------------------------------------


class _FakeSSHException(Exception):
    """paramiko.SSHException Stub."""


class _FakeAuthException(_FakeSSHException):
    """paramiko.AuthenticationException Stub."""


class _FakeKey:
    """paramiko.PKey Stub - akzeptiert jeden String als Key."""

    @classmethod
    def from_private_key(cls, fileobj):
        data = fileobj.read()
        if "-----BEGIN" not in data:
            raise _FakeSSHException("not a key")
        return cls()


class _FakeAutoAddPolicy:
    pass


@dataclass
class _ExecCall:
    command: str
    timeout: float


@dataclass
class _SftpFile:
    path: str
    chmod: int = 0
    written: bytes = b""

    def write(self, data: bytes) -> None:
        self.written += data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@dataclass
class _FakeSftp:
    files: dict[str, _SftpFile] = field(default_factory=dict)

    def file(self, path: str, mode: str) -> _SftpFile:
        f = _SftpFile(path=path)
        self.files[path] = f
        return f

    def chmod(self, path: str, mode: int) -> None:
        if path in self.files:
            self.files[path].chmod = mode

    def close(self) -> None:
        pass


class _FakeChannel:
    def __init__(self, exit_status: int) -> None:
        self._exit = exit_status

    def recv_exit_status(self) -> int:
        return self._exit


class _FakeStream:
    def __init__(self, content: str, channel: _FakeChannel) -> None:
        self._content = content
        self.channel = channel

    def read(self) -> bytes:
        return self._content.encode("utf-8")


@dataclass
class _FakeClient:
    exec_calls: list[_ExecCall] = field(default_factory=list)
    # Mapping: command-prefix -> (rc, stdout, stderr). Erst-Match gewinnt.
    exec_responses: list[tuple[str, int, str, str]] = field(default_factory=list)
    sftp: _FakeSftp = field(default_factory=_FakeSftp)
    closed: bool = False
    connect_calls: list[dict] = field(default_factory=list)
    connect_raises: Exception | None = None

    def set_missing_host_key_policy(self, _policy) -> None:
        pass

    def connect(self, **kwargs) -> None:
        self.connect_calls.append(kwargs)
        if self.connect_raises is not None:
            raise self.connect_raises

    def open_sftp(self) -> _FakeSftp:
        return self.sftp

    def exec_command(self, command: str, timeout: float):
        self.exec_calls.append(_ExecCall(command=command, timeout=timeout))
        for prefix, rc, out, err in self.exec_responses:
            if prefix in command:
                ch = _FakeChannel(rc)
                return None, _FakeStream(out, ch), _FakeStream(err, ch)
        # Default: rc=0, leer
        ch = _FakeChannel(0)
        return None, _FakeStream("", ch), _FakeStream("", ch)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_client() -> _FakeClient:
    return _FakeClient()


@pytest.fixture
def install_fake_paramiko(monkeypatch, fake_client):
    """Erzeugt ein in-memory ``paramiko``-Stub und mountet ihn in sys.modules.

    ``SSHClient()`` liefert immer den Test-fixturen ``fake_client``,
    damit Asserts dagegen einfach sind.
    """
    fake_module = types.ModuleType("paramiko")
    fake_module.SSHException = _FakeSSHException
    fake_module.AuthenticationException = _FakeAuthException
    fake_module.AutoAddPolicy = _FakeAutoAddPolicy
    fake_module.Ed25519Key = _FakeKey
    fake_module.ECDSAKey = _FakeKey
    fake_module.RSAKey = _FakeKey
    fake_module.DSSKey = _FakeKey
    fake_module.SSHClient = lambda: fake_client
    monkeypatch.setitem(sys.modules, "paramiko", fake_module)
    return fake_module


def _make_device(**overrides) -> VaultDevice:
    base = {
        "id": "dev-1",
        "name": "Lab-FW",
        "host": "10.0.0.1",
        "ssh_enabled": True,
        "ssh_user": "root",
        "ssh_private_key_pem": (
            "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n"
        ),
        "ssh_host": "",
        "ssh_port": 22,
    }
    base.update(overrides)
    return VaultDevice(**base)


# ---------------------------------------------------------------------------
# Pure Kommando-Konstruktion (kein paramiko noetig)
# ---------------------------------------------------------------------------


class TestMakeJobId:
    def test_basic(self) -> None:
        assert make_jobid("plan-abc", "12345678-90ab") == "plan-abc-12345678"

    def test_strips_unsafe_chars(self) -> None:
        # Punkt/Slash sind nicht in der safe-list - werden gestrippt
        jobid = make_jobid("plan/with.path", "device:id")
        assert "/" not in jobid
        assert "." not in jobid
        assert ":" not in jobid

    def test_falls_back_when_empty(self) -> None:
        assert make_jobid("", "") == "apply-dev"


class TestValidateJobId:
    def test_ok(self) -> None:
        _validate_jobid("plan-abc-12345678")

    def test_rejects_shell_chars(self) -> None:
        for bad in ["plan;rm", "plan$INJ", "plan abc", "x" * 81, "ab"]:
            with pytest.raises(ValueError):
                _validate_jobid(bad)


class TestDaemonCommand:
    def test_live_mode_contains_restore_and_reboot(self) -> None:
        cmd = _build_daemon_cmd(jobid="plan-dev", window_s=300, dry_marker_only=False)
        assert "daemon -f -p /var/run/cockpit-safety-plan-dev.pid" in cmd
        assert "sleep 300" in cmd
        assert "touch /var/log/cockpit-safety-plan-dev.fired" in cmd
        assert "cp /conf/config.xml.cockpit-safety-plan-dev.xml /conf/config.xml" in cmd
        assert "/sbin/shutdown -r now" in cmd

    def test_dry_mode_only_touches_marker(self) -> None:
        cmd = _build_daemon_cmd(jobid="plan-dev", window_s=20, dry_marker_only=True)
        assert "touch /var/log/cockpit-safety-plan-dev.fired" in cmd
        assert "shutdown" not in cmd
        assert "cp /conf/config.xml" not in cmd

    def test_invalid_jobid_raises(self) -> None:
        with pytest.raises(ValueError):
            _build_daemon_cmd(jobid="bad;rm", window_s=10, dry_marker_only=False)


class TestVerifyCommand:
    def test_includes_pid_file_path(self) -> None:
        cmd = _build_verify_cmd("plan-dev")
        assert "/var/run/cockpit-safety-plan-dev.pid" in cmd
        assert "kill -0" in cmd


class TestDisarmCommand:
    def test_kills_and_removes(self) -> None:
        cmd = _build_disarm_cmd("plan-dev")
        assert "kill -TERM" in cmd
        assert (
            "rm -f /var/run/cockpit-safety-plan-dev.pid "
            "/conf/config.xml.cockpit-safety-plan-dev.xml" in cmd
        )


class TestMarkerCheckCommand:
    def test_returns_fired_or_clean(self) -> None:
        cmd = _build_marker_check_cmd("plan-dev")
        assert "/var/log/cockpit-safety-plan-dev.fired" in cmd
        assert "echo fired" in cmd
        assert "echo clean" in cmd


# ---------------------------------------------------------------------------
# arm() - Connect + SFTP + daemon-start + verify
# ---------------------------------------------------------------------------


class TestArm:
    def test_happy_path(self, install_fake_paramiko, fake_client) -> None:
        fake_client.exec_responses = [
            # Verify-Phase liefert lebendigen PID
            ("pid=", 0, "pid=12345", ""),
        ]
        dev = _make_device()
        res = ssh_safety_net.arm(
            dev,
            jobid="plan-dev",
            pre_apply_xml=b"<xml/>",
            window_s=300,
        )
        assert isinstance(res, ArmResult)
        assert res.success, res.summary
        assert res.pid == 12345
        # Safety-XML wurde geschrieben
        assert "/conf/config.xml.cockpit-safety-plan-dev.xml" in fake_client.sftp.files
        assert (
            fake_client.sftp.files["/conf/config.xml.cockpit-safety-plan-dev.xml"].written
            == b"<xml/>"
        )
        assert fake_client.sftp.files["/conf/config.xml.cockpit-safety-plan-dev.xml"].chmod == 0o600
        # Aufgerufen wurden: pre-cleanup, daemon, verify
        commands = [c.command for c in fake_client.exec_calls]
        assert any("daemon -f -p" in c for c in commands)
        assert fake_client.closed

    def test_returns_error_when_ssh_not_configured(self, install_fake_paramiko) -> None:
        dev = _make_device(ssh_enabled=False)
        res = ssh_safety_net.arm(
            dev,
            jobid="plan-dev",
            pre_apply_xml=b"<xml/>",
            window_s=300,
        )
        assert not res.success
        assert "nicht aktiviert" in res.summary

    def test_returns_error_on_paramiko_missing(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "paramiko", None)
        # Re-Import-Trick: paramiko=None macht den Late-Import in _connect knallen
        dev = _make_device()
        res = ssh_safety_net.arm(
            dev,
            jobid="plan-dev",
            pre_apply_xml=b"<xml/>",
            window_s=300,
        )
        assert not res.success
        assert "paramiko" in res.summary.lower()

    def test_returns_error_when_daemon_fails(self, install_fake_paramiko, fake_client) -> None:
        fake_client.exec_responses = [
            ("daemon -f", 1, "", "daemon: not found"),
        ]
        dev = _make_device()
        res = ssh_safety_net.arm(
            dev,
            jobid="plan-dev",
            pre_apply_xml=b"<xml/>",
            window_s=300,
        )
        assert not res.success
        assert "daemon" in res.summary.lower()

    def test_returns_error_when_pid_missing_after_start(
        self,
        install_fake_paramiko,
        fake_client,
    ) -> None:
        fake_client.exec_responses = [
            ("pid=", 0, "pid=missing", ""),
        ]
        dev = _make_device()
        res = ssh_safety_net.arm(
            dev,
            jobid="plan-dev",
            pre_apply_xml=b"<xml/>",
            window_s=300,
        )
        assert not res.success
        assert "missing" in res.summary.lower() or "wieder weg" in res.summary.lower()

    def test_invalid_jobid_returns_error(self, install_fake_paramiko) -> None:
        dev = _make_device()
        res = ssh_safety_net.arm(
            dev,
            jobid="bad;rm",
            pre_apply_xml=b"<xml/>",
            window_s=300,
        )
        assert not res.success
        assert "jobid" in res.summary.lower()


# ---------------------------------------------------------------------------
# disarm() - Idempotent cleanup
# ---------------------------------------------------------------------------


class TestDisarm:
    def test_happy_path(self, install_fake_paramiko, fake_client) -> None:
        dev = _make_device()
        res = ssh_safety_net.disarm(dev, jobid="plan-dev")
        assert isinstance(res, DisarmResult)
        assert res.success, res.summary

    def test_returns_error_on_nonzero_rc(self, install_fake_paramiko, fake_client) -> None:
        fake_client.exec_responses = [
            ("kill -TERM", 1, "", "permission denied"),
        ]
        dev = _make_device()
        res = ssh_safety_net.disarm(dev, jobid="plan-dev")
        assert not res.success

    def test_ssh_unreachable_returns_error(self, install_fake_paramiko, fake_client) -> None:
        fake_client.connect_raises = OSError("connection refused")
        dev = _make_device()
        res = ssh_safety_net.disarm(dev, jobid="plan-dev")
        assert not res.success
        assert (
            "connection refused" in res.summary.lower() or "nicht moeglich" in res.summary.lower()
        )


# ---------------------------------------------------------------------------
# check_marker() - drei Pfade: success+fired, success+clean, ssh-fail
# ---------------------------------------------------------------------------


class TestCheckMarker:
    def test_fired_triggers_cleanup_when_requested(
        self,
        install_fake_paramiko,
        fake_client,
    ) -> None:
        fake_client.exec_responses = [
            # marker-check first
            ("echo fired", 0, "fired", ""),
        ]
        dev = _make_device()
        res = ssh_safety_net.check_marker(
            dev,
            jobid="plan-dev",
            cleanup_if_found=True,
        )
        assert isinstance(res, MarkerResult)
        assert res.success
        assert res.fired
        # cleanup command muss aufgerufen worden sein
        commands = [c.command for c in fake_client.exec_calls]
        assert any("rm -f /var/log/cockpit-safety-plan-dev.fired" in c for c in commands)

    def test_clean_returns_not_fired(self, install_fake_paramiko, fake_client) -> None:
        fake_client.exec_responses = [
            ("echo fired", 0, "clean", ""),
        ]
        dev = _make_device()
        res = ssh_safety_net.check_marker(dev, jobid="plan-dev")
        assert res.success
        assert not res.fired

    def test_ssh_unreachable_returns_success_false(
        self,
        install_fake_paramiko,
        fake_client,
    ) -> None:
        fake_client.connect_raises = OSError("timeout")
        dev = _make_device()
        res = ssh_safety_net.check_marker(dev, jobid="plan-dev")
        assert not res.success
        assert not res.fired


# ---------------------------------------------------------------------------
# run_test_loop() - End-to-End mit dry-marker-only
# ---------------------------------------------------------------------------


class TestRunTestLoop:
    def test_happy_path_marker_appears(
        self,
        install_fake_paramiko,
        fake_client,
        monkeypatch,
    ) -> None:
        # arm sieht PID lebendig, marker-check sieht "fired"
        fake_client.exec_responses = [
            ("pid=", 0, "pid=42", ""),
            ("echo fired", 0, "fired", ""),
        ]
        # Sleep auf 0 patchen damit der Test schnell ist
        monkeypatch.setattr(ssh_safety_net.time, "sleep", lambda *_: None)
        dev = _make_device()
        res = ssh_safety_net.run_test_loop(dev, window_s=1, extra_wait_s=1)
        assert isinstance(res, TestResult)
        assert res.success, res.summary

    def test_arm_failure_short_circuits(
        self,
        install_fake_paramiko,
        fake_client,
        monkeypatch,
    ) -> None:
        fake_client.exec_responses = [
            ("daemon -f", 1, "", "no daemon binary"),
        ]
        monkeypatch.setattr(ssh_safety_net.time, "sleep", lambda *_: None)
        dev = _make_device()
        res = ssh_safety_net.run_test_loop(dev, window_s=1, extra_wait_s=1)
        assert not res.success
        assert res.failed_step == "arm"

    def test_marker_not_set_reports_wait_step(
        self,
        install_fake_paramiko,
        fake_client,
        monkeypatch,
    ) -> None:
        fake_client.exec_responses = [
            ("pid=", 0, "pid=42", ""),
            ("echo fired", 0, "clean", ""),
        ]
        monkeypatch.setattr(ssh_safety_net.time, "sleep", lambda *_: None)
        dev = _make_device()
        res = ssh_safety_net.run_test_loop(dev, window_s=1, extra_wait_s=1)
        assert not res.success
        assert res.failed_step == "wait"
