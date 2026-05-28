"""Hauptfenster: Tab-Routing, Aktions-Menü, Inaktivitäts-Timer, Plan→Apply-Flow."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabWidget,
)

from opn_cockpit.audit.log import AuditEventKind, AuditLog, default_audit_path
from opn_cockpit.config import AppSettings, get_app_data_dir
from opn_cockpit.core.health import check_device
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning
from opn_cockpit.core.objects.aliases import AliasSpec
from opn_cockpit.core.objects.routes import RouteSpec
from opn_cockpit.gui.action_dialogs.add_alias import AddAliasDialog
from opn_cockpit.gui.action_dialogs.add_route import AddRouteDialog
from opn_cockpit.gui.action_dialogs.device_form import DeviceFormDialog
from opn_cockpit.gui.audit_view import AuditView
from opn_cockpit.gui.inventory_view import InventoryView
from opn_cockpit.gui.preview_dialog import PreviewDialog
from opn_cockpit.gui.result_dialog import ResultDialog
from opn_cockpit.inventory.model import Device
from opn_cockpit.inventory.store import InventoryStore
from opn_cockpit.orchestration.executor import Executor
from opn_cockpit.orchestration.plan_store import PlanStore
from opn_cockpit.orchestration.planner import Planner
from opn_cockpit.orchestration.registry import get_binding
from opn_cockpit.security.session import Session
from opn_cockpit.vault.errors import InvalidPasswordError
from opn_cockpit.vault.model import VaultDevice
from opn_cockpit.vault.store import save_vault

INACTIVITY_TICK_MS = 30_000  # 30 s


class MainWindow(QMainWindow):
    """Tab-basierte Hauptansicht."""

    def __init__(
        self,
        *,
        session: Session,
        app_settings: AppSettings,
    ) -> None:
        super().__init__()
        self.setWindowTitle(self._title_for(session.vault_path))
        self.resize(1100, 700)

        self._session = session
        self._app_settings = app_settings
        self._audit = AuditLog(path=default_audit_path())
        self._inventory = InventoryStore(session=session)
        self._plan_store = PlanStore(base_dir=get_app_data_dir() / "plans")

        # --- Tabs ---
        self._tabs = QTabWidget(self)
        self._inventory_view = InventoryView(
            inventory=self._inventory,
            on_add=self._open_add_device,
            on_remove=self._remove_device,
            on_test=self._test_connections,
            parent=self,
        )
        self._tabs.addTab(self._inventory_view, "Inventar")
        self._audit_view = AuditView(self._audit, self)
        self._tabs.addTab(self._audit_view, "Audit-Log")
        self.setCentralWidget(self._tabs)

        # --- Menü ---
        self._build_menu()

        # --- Statusleiste ---
        self._status = QStatusBar(self)
        self.setStatusBar(self._status)
        self._update_status()

        # --- Inaktivitäts-Timer (R-SEC-6) ---
        self._inactivity_timer = QTimer(self)
        self._inactivity_timer.timeout.connect(self._on_inactivity_tick)
        self._inactivity_timer.start(INACTIVITY_TICK_MS)

    # ----- Menü-Aufbau -----

    def _build_menu(self) -> None:
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&Datei")
        act_lock = QAction("Tresor sperren", self)
        act_lock.triggered.connect(self._lock_now)
        file_menu.addAction(act_lock)
        file_menu.addSeparator()
        act_quit = QAction("Beenden", self)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        actions_menu = menubar.addMenu("&Aktionen")
        act_route = QAction("Route hinzufügen…", self)
        act_route.triggered.connect(self._open_add_route_dialog)
        actions_menu.addAction(act_route)
        act_alias = QAction("Alias hinzufügen / erweitern…", self)
        act_alias.triggered.connect(self._open_add_alias_dialog)
        actions_menu.addAction(act_alias)

    # ----- Inaktivität -----

    def _on_inactivity_tick(self) -> None:
        if self._session.check_inactivity():
            self._audit.append(
                AuditEventKind.SESSION_AUTO_LOCKED,
                vault_path=str(self._session.vault_path) if self._session.vault_path else None,
                summary="Inaktivitätssperre aktiv — Tresor gesperrt.",
            )
            QMessageBox.information(
                self,
                "Sperre",
                "Tresor wurde wegen Inaktivität gesperrt.",
            )
            self.close()
        else:
            self._update_status()

    def _update_status(self) -> None:
        remaining = self._session.seconds_until_expiry()
        mins = int(remaining // 60)
        secs = int(remaining % 60)
        self._status.showMessage(
            f"Tresor entsperrt — automatische Sperre in {mins:02d}:{secs:02d}"
        )

    def _lock_now(self) -> None:
        self._session.lock()
        self._audit.append(
            AuditEventKind.VAULT_LOCKED,
            summary="Tresor manuell gesperrt.",
        )
        self.close()

    # ----- Device-Operationen -----

    def _open_add_device(self) -> None:
        dlg = DeviceFormDialog(self)
        if dlg.exec() != DeviceFormDialog.DialogCode.Accepted:
            return
        form = dlg.result_data()
        if form is None:
            return
        vault_device = VaultDevice(
            id=VaultDevice.new_id(),
            name=form.name,
            host=form.host,
            port=form.port,
            tls_verify=form.tls_verify,
            tags=form.tags,
            api_key=form.api_key,
            api_secret=form.api_secret,
            descr=form.descr,
        )
        self._session.opened.data.devices.append(vault_device)
        if not self._save_vault_interactive():
            # Roll back
            self._session.opened.data.devices.pop()
            return
        self._inventory_view.refresh()
        self._session.touch()

    def _remove_device(self, device: Device) -> None:
        confirm = QMessageBox.question(
            self,
            "Gerät entfernen",
            f"Gerät '{device.name}' wirklich aus dem Tresor entfernen?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        removed_index = None
        for idx, vd in enumerate(self._session.opened.data.devices):
            if vd.id == device.id:
                removed_index = idx
                break
        if removed_index is None:
            return
        backup = self._session.opened.data.devices.pop(removed_index)
        if not self._save_vault_interactive():
            self._session.opened.data.devices.insert(removed_index, backup)
            return
        self._inventory_view.refresh()
        self._session.touch()

    def _save_vault_interactive(self) -> bool:
        path = self._session.vault_path
        if path is None:
            return False
        password, ok = QInputDialog.getText(
            self,
            "Master-Passwort",
            f"Master-Passwort zum Speichern in {path}:",
            QLineEdit.EchoMode.Password,
        )
        if not ok or not password:
            return False
        try:
            new_opened = save_vault(path, self._session.opened, password)
        except InvalidPasswordError:
            QMessageBox.critical(
                self, "Passwort falsch", "Master-Passwort falsch — nicht gespeichert."
            )
            return False
        except Exception as exc:
            QMessageBox.critical(self, "Speichern fehlgeschlagen", str(exc))
            return False
        self._session.replace_opened(new_opened)
        return True

    # ----- Test-Verbindung -----

    def _test_connections(self, devices: Iterable[Device]) -> None:
        devices_list = list(devices)
        if not devices_list:
            QMessageBox.information(self, "Verbindungstest", "Keine Geräte ausgewählt.")
            return
        tuning = _tuning(self._session)
        targets = [
            HttpTarget(host=d.host, port=d.port, verify=d.tls_verify)
            for d in devices_list
        ]
        lines: list[str] = []
        with HttpClient(targets=targets, tuning=tuning) as client:
            for device, target in zip(devices_list, targets, strict=True):
                try:
                    key, secret = self._session.credentials_for(device.id)
                except Exception:
                    lines.append(f"{device.name}: keine Credentials")
                    continue
                result = check_device(client, target, key, secret)
                status = "OK" if result.is_ok else ("NO-AUTH" if result.reachable else "OFFLINE")
                lines.append(f"{device.name}: {status} — {result.summary}")
        QMessageBox.information(self, "Verbindungstest", "\n".join(lines))
        self._session.touch()

    # ----- Aktions-Dialoge -----

    def _open_add_route_dialog(self) -> None:
        dlg = AddRouteDialog(self)
        if dlg.exec() != AddRouteDialog.DialogCode.Accepted:
            return
        result = dlg.result_data()
        if result is None:
            return
        self._plan_and_apply(
            action_name="add_route",
            subsystem="routes",
            spec=result.spec,
            selector=result.selector,
        )

    def _open_add_alias_dialog(self) -> None:
        dlg = AddAliasDialog(self)
        if dlg.exec() != AddAliasDialog.DialogCode.Accepted:
            return
        result = dlg.result_data()
        if result is None:
            return
        action_name = "append_alias" if result.spec.merge_mode == "append" else "add_alias"
        self._plan_and_apply(
            action_name=action_name,
            subsystem="firewall_alias",
            spec=result.spec,
            selector=result.selector,
        )

    # ----- Plan + Apply Flow -----

    def _plan_and_apply(
        self,
        *,
        action_name: str,
        subsystem: str,
        spec: RouteSpec | AliasSpec | Any,
        selector: str,
    ) -> None:
        devices = self._inventory.select(selector)
        if not devices:
            QMessageBox.warning(
                self, "Auswahl leer",
                f"Selektor '{selector}' liefert keine Geräte.",
            )
            return
        binding = get_binding(subsystem)
        tuning = _tuning(self._session)
        targets = [HttpTarget(host=d.host, port=d.port, verify=d.tls_verify) for d in devices]

        planner = Planner(
            audit=self._audit,
            session=self._session,
            max_workers=self._session.opened.data.settings.max_workers,
        )
        with HttpClient(targets=targets, tuning=tuning) as client:
            try:
                plan = planner.create_plan(
                    action=action_name, spec=spec,
                    devices=devices, adapter=binding.adapter, client=client,
                )
            except Exception as exc:
                QMessageBox.critical(self, "Plan-Erzeugung fehlgeschlagen", str(exc))
                return
            self._plan_store.save(plan)

            preview = PreviewDialog(plan, self)
            if preview.exec() != PreviewDialog.DialogCode.Accepted:
                self._session.touch()
                return

            executor = Executor(
                session=self._session,
                audit=self._audit,
                max_workers=self._session.opened.data.settings.max_workers,
            )
            try:
                report = executor.apply(
                    plan, adapter=binding.adapter, controller=binding.controller,
                    client=client,
                )
            except Exception as exc:
                QMessageBox.critical(self, "Apply fehlgeschlagen", str(exc))
                return

        ResultDialog(
            report=report,
            device_labels={d.id: d.name for d in devices},
            parent=self,
        ).exec()
        # Audit-View aktualisieren
        self._audit_view.refresh()
        self._session.touch()

    # ----- Hilfen -----

    @staticmethod
    def _title_for(path: Path | None) -> str:
        if path is None:
            return "OPN-Cockpit"
        return f"OPN-Cockpit — {path}"


def _tuning(session: Session) -> HttpTuning:
    settings = session.opened.data.settings
    return HttpTuning(
        connect_timeout_s=settings.connect_timeout_s,
        read_timeout_s=settings.read_timeout_s,
        reconfigure_timeout_s=settings.reconfigure_timeout_s,
        retry_count=settings.retry_count,
    )


__all__ = ["MainWindow"]
