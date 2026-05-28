"""Vorschau-Dialog: zeigt den Plan, verlangt explizite Bestätigung (R-PRE-2)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from opn_cockpit.orchestration.planner import Plan
from opn_cockpit.orchestration.reporter import format_plan_preview, format_plan_summary


class PreviewDialog(QDialog):
    """Plan-Vorschau mit Bestätigungs-Checkbox.

    Der "Ausrollen"-Button bleibt deaktiviert, solange die Checkbox nicht
    aktiviert ist — verhindert R-PRE-2-Verletzung durch versehentlichen Klick.
    """

    def __init__(self, plan: Plan, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Plan-Vorschau — {plan.plan_id}")
        self.setModal(True)
        self.resize(900, 600)

        # --- Zusammenfassung oben ---
        summary = QLabel(format_plan_summary(plan))
        summary.setStyleSheet("font-weight: bold; padding: 6px;")

        # --- Vorschau-Text ---
        preview = QPlainTextEdit()
        preview.setReadOnly(True)
        preview.setPlainText(format_plan_preview(plan))
        preview.setStyleSheet("font-family: Consolas, monospace;")

        # --- Bestätigungs-Checkbox ---
        self._confirm = QCheckBox(
            "Ich habe die Vorschau geprüft und möchte ausrollen."
        )

        # --- Buttons ---
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if self._ok_btn is not None:
            self._ok_btn.setText("Ausrollen")
            self._ok_btn.setEnabled(False)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        self._confirm.toggled.connect(self._on_confirm_toggled)

        # --- Layout ---
        layout = QVBoxLayout(self)
        layout.addWidget(summary)
        layout.addWidget(preview, 1)
        layout.addWidget(self._confirm)
        layout.addWidget(self._buttons)

        self._confirm.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_confirm_toggled(self, checked: bool) -> None:
        if self._ok_btn is not None:
            self._ok_btn.setEnabled(checked)
