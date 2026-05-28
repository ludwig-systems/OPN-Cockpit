"""Smoke-Tests für die GUI-Schicht.

Verifiziert nur, dass die wichtigsten Widgets ohne Fehler konstruiert
werden können und dass die Maskierung im Excepthook funktioniert.
Render- und Interaktions-Tests bleiben dem Live-Test vorbehalten.
"""

from __future__ import annotations

import sys

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
from opn_cockpit.gui.login_dialog import LoginDialog
from opn_cockpit.gui.preview_dialog import PreviewDialog
from opn_cockpit.gui.result_dialog import ResultDialog
from opn_cockpit.gui.widgets.badges import tls_badge
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
    def test_constructs_without_error(self, qapp: QApplication) -> None:
        dlg = LoginDialog(default_path="C:\\test.opnvault")
        assert dlg.windowTitle().startswith("OPN-Cockpit")


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


class TestMaskingExcepthook:
    def test_install_and_restore(self) -> None:
        original = sys.excepthook
        install_masking_excepthook()
        assert sys.excepthook is not original
        restore_excepthook()
        assert sys.excepthook is original
