"""Inventar-Sicht: Tabelle der Geräte aus dem entsperrten Tresor.

Liefert auch Hilfen für die Action-Dialoge:

* :meth:`selected_devices` — Auswahl, gegen die eine Aktion ausgerollt wird
* :meth:`refresh` — Tabelle nach Inventar-Änderungen neu aufbauen
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
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

from opn_cockpit.gui.widgets.badges import tls_badge
from opn_cockpit.inventory.model import Device
from opn_cockpit.inventory.store import InventoryStore


class InventoryView(QWidget):
    """Tabellen-Widget mit Geräten + Schaltflächen."""

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

        self._table = QTableWidget(0, 5, self)
        self._table.setHorizontalHeaderLabels(
            ["Name", "Host", "Port", "TLS", "Tags"]
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        # --- Buttons ---
        add_btn = QPushButton("Gerät hinzufügen…")
        add_btn.clicked.connect(self._add_clicked)
        remove_btn = QPushButton("Ausgewähltes entfernen")
        remove_btn.clicked.connect(self._remove_clicked)
        test_btn = QPushButton("Verbindung testen")
        test_btn.clicked.connect(self._test_clicked)

        btn_row = QHBoxLayout()
        btn_row.addWidget(add_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addWidget(test_btn)
        btn_row.addStretch()

        # --- Layout ---
        layout = QVBoxLayout(self)
        layout.addWidget(self._table)
        layout.addLayout(btn_row)

        self.refresh()

    # ----- Public API -----

    def refresh(self) -> None:
        devices = self._inventory.list_devices()
        self._table.setRowCount(len(devices))
        for row_idx, device in enumerate(devices):
            self._table.setItem(row_idx, 0, _item(device.name, device))
            self._table.setItem(row_idx, 1, _item(device.host))
            self._table.setItem(row_idx, 2, _item(str(device.port)))
            self._table.setCellWidget(row_idx, 3, tls_badge(device.tls_verify))
            self._table.setItem(row_idx, 4, _item(", ".join(device.tags)))

    def selected_devices(self) -> list[Device]:
        selected_rows = sorted({idx.row() for idx in self._table.selectedIndexes()})
        result: list[Device] = []
        for row in selected_rows:
            item = self._table.item(row, 0)
            if item is None:
                continue
            device = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(device, Device):
                result.append(device)
        return result

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


def _item(text: str, device: Device | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if device is not None:
        item.setData(Qt.ItemDataRole.UserRole, device)
    return item
