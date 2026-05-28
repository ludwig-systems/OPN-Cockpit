"""Result-Matrix-Dialog: Anzeige des Rollout-Berichts nach Apply."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from opn_cockpit.core.result import RolloutReport
from opn_cockpit.orchestration.reporter import (
    format_rollout_matrix,
    format_rollout_summary,
)


class ResultDialog(QDialog):
    """Zeigt die Result-Matrix nach Abschluss eines ``apply``."""

    def __init__(
        self,
        report: RolloutReport,
        device_labels: dict[str, str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Rollout-Ergebnis")
        self.setModal(True)
        self.resize(900, 500)

        summary = QLabel(format_rollout_summary(report))
        summary.setStyleSheet(
            "padding: 6px; font-weight: bold; "
            + ("color: #2e7d32;" if report.failures == 0 else "color: #c62828;")
        )

        matrix = QPlainTextEdit()
        matrix.setReadOnly(True)
        matrix.setPlainText(
            format_rollout_matrix(report, devices_by_id=device_labels)
        )
        matrix.setStyleSheet("font-family: Consolas, monospace;")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(summary)
        layout.addWidget(matrix, 1)
        layout.addWidget(buttons)
