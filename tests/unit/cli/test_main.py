"""Smoke-Tests für die CLI — Parser-Aufbau + ausgewählte Sub-Commands.

Die wirklich harte End-to-End-Validierung (Plan + Apply gegen MockTransport)
deckt der ``test_executor.py``-Stack ab. Hier prüfen wir nur, dass:

* der Parser alle geplanten Sub-Commands kennt,
* ``list-devices`` korrekt mit einem entsperrten Tresor läuft,
* ``audit`` das richtige Audit-Log liest,
* Wrong-Password-Pfade einen klaren Exit-Code liefern.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from opn_cockpit.audit.log import AuditEventKind, AuditLog
from opn_cockpit.cli.main import build_parser, main
from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault

PASSWORD = "korrektes-pferd-batterie-heftklammer"


@pytest.fixture(autouse=True)
def _patch_app_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Zwingt sowohl AppSettings als auch das Audit-Default-Path in tmp."""
    fake_appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(fake_appdata))
    return fake_appdata


def _create_vault_with(devices: list[VaultDevice], tmp_path: Path) -> Path:
    path = tmp_path / "v.opnvault"
    create_vault(path, PASSWORD, VaultData(devices=devices))
    return path


class TestParser:
    def test_parser_contains_all_subcommands(self) -> None:
        parser = build_parser()
        help_text = parser.format_help()
        for sub in (
            "create-vault", "change-password", "export-template",
            "list-devices", "add-device", "remove-device",
            "test-connection", "plan", "apply", "audit",
        ):
            assert sub in help_text

    def test_no_subcommand_prints_help_and_returns_nonzero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main([])
        assert code != 0
        captured = capsys.readouterr()
        assert "COMMAND" in captured.err


class TestListDevices:
    def test_lists_existing_devices(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        vault = _create_vault_with(
            [
                VaultDevice(
                    id="id-1", name="Berlin", host="opn-berlin.lab",
                    api_key="K", api_secret="S",
                )
            ],
            tmp_path,
        )
        with patch("opn_cockpit.cli.main.prompt_password", return_value=PASSWORD):
            code = main(["--vault", str(vault), "list-devices"])
        assert code == 0
        out = capsys.readouterr().out
        assert "Berlin" in out
        assert "opn-berlin.lab" in out
        # Niemals API-Secret im stdout
        assert "S" not in out.split()  # einzelnes "S" ungültig — defensiv prüfen
        assert "api_secret" not in out

    def test_empty_inventory_message(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        vault = _create_vault_with([], tmp_path)
        with patch("opn_cockpit.cli.main.prompt_password", return_value=PASSWORD):
            code = main(["--vault", str(vault), "list-devices"])
        assert code == 0
        out = capsys.readouterr().out
        assert "keine" in out.lower()

    def test_wrong_password_returns_auth_error_code(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        vault = _create_vault_with([], tmp_path)
        with patch(
            "opn_cockpit.cli.main.prompt_password",
            return_value="Falsches-Passwort-1234",
        ):
            code = main(["--vault", str(vault), "list-devices"])
        # 3 = EXIT_AUTH_ERROR
        assert code == 3


class TestAudit:
    def test_lists_existing_records(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Audit-Log via Default-Pfad ablegen
        audit_path = tmp_path / "appdata" / "OPN-Cockpit" / "audit.jsonl"
        audit = AuditLog(path=audit_path, actor="testuser")
        audit.append(AuditEventKind.VAULT_OPENED, summary="erstmal aufgemacht")
        code = main(["audit"])
        assert code == 0
        out = capsys.readouterr().out
        assert "testuser" in out
        assert "vault_opened" in out

    def test_event_filter_unknown_returns_error(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        code = main(["audit", "--event", "not-a-real-event"])
        assert code != 0
        err = capsys.readouterr().err
        assert "Unbekannter event-Wert" in err

    def test_filter_by_action(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        audit_path = tmp_path / "appdata" / "OPN-Cockpit" / "audit.jsonl"
        audit = AuditLog(path=audit_path, actor="u")
        audit.append(AuditEventKind.DEVICE_RESULT, action="add_route", summary="r")
        audit.append(AuditEventKind.DEVICE_RESULT, action="other", summary="o")
        code = main(["audit", "--action", "add_route"])
        assert code == 0
        out = capsys.readouterr().out
        assert "add_route" in out
        assert "other" not in out


class TestCreateVault:
    def test_creates_vault_file_and_audits(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = tmp_path / "neu.opnvault"
        with patch(
            "opn_cockpit.cli.main.prompt_password_with_confirmation",
            return_value=PASSWORD,
        ):
            code = main(["create-vault", str(target)])
        assert code == 0
        assert target.exists()
        out = capsys.readouterr().out
        assert "angelegt" in out

    def test_refuses_to_overwrite(self, tmp_path: Path) -> None:
        target = tmp_path / "vorhanden.opnvault"
        target.write_bytes(b"X")
        code = main(["create-vault", str(target)])
        assert code != 0
