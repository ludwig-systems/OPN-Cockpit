"""Audit-Sicht: filterbare Tabelle des persistenten JSON-Lines-Logs."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from opn_cockpit.audit.log import AuditEventKind, AuditLog


class AuditView(QWidget):
    """Filterleiste + Tabelle mit Einträgen aus :class:`AuditLog`."""

    _ALL_EVENT_LABEL = "(alle)"

    def __init__(self, audit: AuditLog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._audit = audit

        # --- Filter ---
        self._event_combo = QComboBox()
        self._event_combo.addItem(self._ALL_EVENT_LABEL, None)
        for kind in AuditEventKind:
            self._event_combo.addItem(str(kind), kind)

        self._action_edit = QLineEdit()
        self._action_edit.setPlaceholderText("z. B. add_route")
        self._device_edit = QLineEdit()
        self._device_edit.setPlaceholderText("Geräte-ID")
        self._limit_edit = QLineEdit("200")

        apply_btn = QPushButton("Filtern")
        apply_btn.clicked.connect(self.refresh)
        reset_btn = QPushButton("Zurücksetzen")
        reset_btn.clicked.connect(self._reset_filters)

        form = QFormLayout()
        form.addRow("Event:", self._event_combo)
        form.addRow("Aktion:", self._action_edit)
        form.addRow("Geräte-ID:", self._device_edit)
        form.addRow("Limit:", self._limit_edit)

        btn_row = QHBoxLayout()
        btn_row.addWidget(apply_btn)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch()

        # --- Tabelle ---
        self._table = QTableWidget(0, 6, self)
        self._table.setHorizontalHeaderLabels(
            ["Zeit (UTC)", "Akteur", "Event", "Aktion", "Status", "Hinweis"]
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.Stretch,
        )

        # --- Layout ---
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(btn_row)
        layout.addWidget(self._table)

        self.refresh()

    # ----- Slots -----

    def refresh(self) -> None:
        try:
            limit = int(self._limit_edit.text())
        except ValueError:
            limit = 200
        event = self._event_combo.currentData()
        action = self._action_edit.text().strip() or None
        device_id = self._device_edit.text().strip() or None

        records = self._audit.filter(
            event=event,
            action=action,
            target_device_id=device_id,
        )
        if limit > 0:
            records = records[-limit:]

        self._table.setRowCount(len(records))
        for row_idx, rec in enumerate(records):
            self._table.setItem(row_idx, 0, _item(rec.timestamp_utc))
            self._table.setItem(row_idx, 1, _item(rec.actor))
            self._table.setItem(row_idx, 2, _item(str(rec.event)))
            self._table.setItem(row_idx, 3, _item(rec.action or "-"))
            self._table.setItem(row_idx, 4, _item(rec.status or ""))
            self._table.setItem(row_idx, 5, _item(rec.summary))

    def _reset_filters(self) -> None:
        self._event_combo.setCurrentIndex(0)
        self._action_edit.clear()
        self._device_edit.clear()
        self._limit_edit.setText("200")
        self.refresh()


def _item(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setData(Qt.ItemDataRole.UserRole, text)
    return item
