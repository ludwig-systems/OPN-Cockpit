"""Inventar-Sicht: Tabelle der Geräte aus dem entsperrten Tresor.

Liefert auch Hilfen für die Action-Dialoge:

* :meth:`selected_devices` — Auswahl, gegen die eine Aktion ausgerollt wird
* :meth:`refresh` — Tabelle nach Inventar-Änderungen neu aufbauen

v1.1: Erreichbarkeits-Heartbeat. Pro Gerät wird in einem Hintergrund-Thread
ein TCP-Probe gemacht und das Ergebnis als Pünktchen-Badge in der ersten
Spalte angezeigt — kein Auth, keine API-Last, nur schneller Sichtbarkeits-
Indikator.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from opn_cockpit.gui.widgets.badges import ReachabilityBadge, tls_badge
from opn_cockpit.gui.widgets.reachability_worker import ReachabilityProbe
from opn_cockpit.inventory.model import Device
from opn_cockpit.inventory.store import InventoryStore

REACHABILITY_TICK_MS = 30_000  # 30 s — Background-Heartbeat


class InventoryView(QWidget):
    """Tabellen-Widget mit Geräten + Schaltflächen + Erreichbarkeits-Heartbeat."""

    # Spaltenkonstanten (geordnet wie in der Tabelle)
    COL_STATUS = 0
    COL_NAME = 1
    COL_HOST = 2
    COL_PORT = 3
    COL_TLS = 4
    COL_TAGS = 5

    def __init__(
        self,
        inventory: InventoryStore,
        *,
        on_add: Callable[[], None],
        on_remove: Callable[[Device], None],
        on_test: Callable[[list[Device]], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._inventory = inventory
        self._on_add = on_add
        self._on_remove = on_remove
        self._on_test = on_test
        self._badges: dict[str, ReachabilityBadge] = {}
        self._active_probe: ReachabilityProbe | None = None

        self._table = QTableWidget(0, 6, self)
        self._table.setHorizontalHeaderLabels(
            ["●", "Name", "Host", "Port", "TLS", "Tags"]
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_PORT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_TLS, QHeaderView.ResizeMode.ResizeToContents)

        # --- Buttons ---
        add_btn = QPushButton("Gerät hinzufügen…")
        add_btn.clicked.connect(self._add_clicked)
        remove_btn = QPushButton("Ausgewähltes entfernen")
        remove_btn.clicked.connect(self._remove_clicked)
        test_btn = QPushButton("Verbindung testen")
        test_btn.clicked.connect(self._test_clicked)
        probe_btn = QPushButton("Heartbeat jetzt prüfen")
        probe_btn.clicked.connect(self.trigger_reachability_probe)

        btn_row = QHBoxLayout()
        btn_row.addWidget(add_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addWidget(test_btn)
        btn_row.addWidget(probe_btn)
        btn_row.addStretch()

        # --- Layout ---
        layout = QVBoxLayout(self)
        layout.addWidget(self._table)
        layout.addLayout(btn_row)

        # --- Heartbeat-Timer ---
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.timeout.connect(self.trigger_reachability_probe)
        self._heartbeat_timer.start(REACHABILITY_TICK_MS)

        self.refresh()

    # ----- Public API -----

    def refresh(self) -> None:
        """Baut die Tabelle neu auf und triggert einen frischen Probe-Lauf."""
        devices = self._inventory.list_devices()
        self._badges.clear()
        self._table.setRowCount(len(devices))
        for row_idx, device in enumerate(devices):
            badge = ReachabilityBadge(host=device.host, port=device.port)
            self._badges[device.id] = badge
            self._table.setCellWidget(row_idx, self.COL_STATUS, badge)
            self._table.setItem(row_idx, self.COL_NAME, _item(device.name, device))
            self._table.setItem(row_idx, self.COL_HOST, _item(device.host))
            self._table.setItem(row_idx, self.COL_PORT, _item(str(device.port)))
            self._table.setCellWidget(row_idx, self.COL_TLS, tls_badge(device.tls_verify))
            self._table.setItem(row_idx, self.COL_TAGS, _item(", ".join(device.tags)))
        # Direkt nach refresh einen ersten Probe-Lauf anwerfen, damit der
        # User nicht 30 s lang nur graue Ringe sieht.
        if devices:
            self.trigger_reachability_probe()

    def selected_devices(self) -> list[Device]:
        selected_rows = sorted({idx.row() for idx in self._table.selectedIndexes()})
        result: list[Device] = []
        for row in selected_rows:
            item = self._table.item(row, self.COL_NAME)
            if item is None:
                continue
            device = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(device, Device):
                result.append(device)
        return result

    def trigger_reachability_probe(self) -> None:
        """Stoesst einen einmaligen Probe-Lauf an.

        Wenn ein Lauf gerade laeuft, wird der neue Trigger verworfen — wir
        wollen den OPNsense-Lab-Switch nicht mit redundanten Connects fluten,
        wenn der User mehrfach auf den Heartbeat-Knopf klickt.
        """
        if self._active_probe is not None:
            return
        devices = self._inventory.list_devices()
        if not devices:
            return
        for device in devices:
            badge = self._badges.get(device.id)
            if badge is not None:
                badge.set_state("probing")
        probe = ReachabilityProbe(devices=devices, parent=self)
        probe.results_ready.connect(self._on_probe_results)
        self._active_probe = probe
        probe.start()

    # ----- Slots -----

    def _add_clicked(self) -> None:
        self._on_add()

    def _remove_clicked(self) -> None:
        devices = self.selected_devices()
        if not devices:
            return
        for device in devices:
            self._on_remove(device)
        self.refresh()

    def _test_clicked(self) -> None:
        devices = self.selected_devices() or self._inventory.list_devices()
        self._on_test(devices)

    def _on_probe_results(self, results: dict[str, bool]) -> None:
        for device_id, reachable in results.items():
            badge = self._badges.get(device_id)
            if badge is not None:
                badge.set_state("ok" if reachable else "fail")
        self._active_probe = None


def _item(text: str, device: Device | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if device is not None:
        item.setData(Qt.ItemDataRole.UserRole, device)
    return item
