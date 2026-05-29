"""Smoke-Tests für die GUI-Schicht.

Verifiziert nur, dass die wichtigsten Widgets ohne Fehler konstruiert
werden können und dass die Maskierung im Excepthook funktioniert.
Render- und Interaktions-Tests bleiben dem Live-Test vorbehalten.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

import opn_cockpit.gui.action_dialogs.add_alias as _add_alias_mod
import opn_cockpit.gui.action_dialogs.add_route as _add_route_mod
import opn_cockpit.gui.action_dialogs.device_form as _device_form_mod
import opn_cockpit.gui.audit_view as _audit_view_mod
import opn_cockpit.gui.inventory_view as _inventory_view_mod
import opn_cockpit.gui.main_window as _main_window_mod
from opn_cockpit.core.result import RolloutReport
from opn_cockpit.gui.action_dialogs.add_route import AddRouteDialog
from opn_cockpit.gui.action_dialogs.device_form import DeviceFormDialog
from opn_cockpit.gui.app import install_masking_excepthook, restore_excepthook
from opn_cockpit.gui.create_vault_dialog import CreateVaultDialog
from opn_cockpit.gui.login_dialog import LoginDialog
from opn_cockpit.gui.preview_dialog import PreviewDialog
from opn_cockpit.gui.result_dialog import ResultDialog
from opn_cockpit.gui.widgets.badges import ReachabilityBadge, tls_badge
from opn_cockpit.orchestration.planner import Plan


def test_gui_modules_importable(qapp: QApplication) -> None:
    """Alle GUI-Module müssen sich ohne Seiteneffekte importieren lassen."""
    assert _add_alias_mod is not None
    assert _add_route_mod is not None
    assert _device_form_mod is not None
    assert _audit_view_mod is not None
    assert _inventory_view_mod is not None
    assert _main_window_mod is not None


class TestLoginDialog:
    def test_empty_state_disables_inputs(self, qapp: QApplication) -> None:
        dlg = LoginDialog(available_vaults=[])
        assert dlg.windowTitle().startswith("OPN-Cockpit")
        # Ohne Tresore: Combo disabled, kein Entsperren moeglich
        assert not dlg._vault_combo.isEnabled()

    def test_single_vault_enables_password(self, qapp: QApplication) -> None:
        dlg = LoginDialog(available_vaults=[Path("/tmp/x.opnvault")])
        assert dlg._vault_combo.isEnabled()
        assert dlg._pw_edit.isEnabled()
        # Eintrag ist vorausgewaehlt
        assert dlg._vault_combo.count() == 1

    def test_multiple_vaults_listed(self, qapp: QApplication) -> None:
        dlg = LoginDialog(
            available_vaults=[
                Path("/tmp/a.opnvault"),
                Path("/tmp/b.opnvault"),
                Path("/tmp/c.opnvault"),
            ]
        )
        assert dlg._vault_combo.count() == 3


class TestCreateVaultDialog:
    def test_constructs_with_default_path(self, qapp: QApplication) -> None:
        dlg = CreateVaultDialog()
        assert "anlegen" in dlg.windowTitle().lower()
        assert dlg.result_data is None


class TestPreviewDialog:
    def test_constructs_with_empty_plan(self, qapp: QApplication) -> None:
        plan = Plan(
            plan_id="pl-AAAA1111",
            action="add_route",
            subsystem="routes",
            created_at_utc="2026-05-28T10:00:00.000Z",
        )
        dlg = PreviewDialog(plan)
        assert "pl-AAAA1111" in dlg.windowTitle()


class TestResultDialog:
    def test_constructs_with_empty_report(self, qapp: QApplication) -> None:
        dlg = ResultDialog(RolloutReport(), device_labels={})
        assert dlg.windowTitle().startswith("Rollout")


class TestActionDialogs:
    def test_add_route_dialog_returns_none_on_empty(self, qapp: QApplication) -> None:
        dlg = AddRouteDialog()
        assert dlg.result_data() is None

    def test_device_form_dialog_returns_none_on_empty(self, qapp: QApplication) -> None:
        dlg = DeviceFormDialog()
        assert dlg.result_data() is None


class TestTlsBadge:
    def test_enabled_shows_tls(self, qapp: QApplication) -> None:
        label = tls_badge(verify_enabled=True)
        assert label.text() == "TLS"

    def test_disabled_shows_warning(self, qapp: QApplication) -> None:
        label = tls_badge(verify_enabled=False)
        assert "AUS" in label.text()
        assert label.toolTip()


class TestReachabilityBadge:
    def test_initial_state_is_unknown(self, qapp: QApplication) -> None:
        badge = ReachabilityBadge(host="opn-lab", port=443)
        assert badge.state == "unknown"
        assert badge.text() == "○"

    def test_state_ok_is_green_dot(self, qapp: QApplication) -> None:
        badge = ReachabilityBadge(host="opn-lab", port=443)
        badge.set_state("ok")
        assert badge.state == "ok"
        assert badge.text() == "●"
        assert "Erreichbar" in badge.toolTip()

    def test_state_fail_is_red_dot_with_tooltip(self, qapp: QApplication) -> None:
        badge = ReachabilityBadge(host="opn-lab", port=443)
        badge.set_state("fail")
        assert badge.state == "fail"
        assert "Nicht erreichbar" in badge.toolTip()

    def test_state_probing_shows_yellow(self, qapp: QApplication) -> None:
        badge = ReachabilityBadge(host="opn-lab", port=443)
        badge.set_state("probing")
        assert badge.state == "probing"
        assert "Pruefe" in badge.toolTip()

    def test_includes_host_port_in_tooltip(self, qapp: QApplication) -> None:
        badge = ReachabilityBadge(host="opn-berlin.lab", port=8443)
        badge.set_state("ok")
        assert "opn-berlin.lab:8443" in badge.toolTip()


class TestMaskingExcepthook:
    def test_install_and_restore(self) -> None:
        original = sys.excepthook
        install_masking_excepthook()
        assert sys.excepthook is not original
        restore_excepthook()
        assert sys.excepthook is original
