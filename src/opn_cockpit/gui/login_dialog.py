"""Login-Dialog: Tresor-Pfad-Auswahl + Master-Passwort.

Modal vor dem Hauptfenster. Liefert ``LoginResult`` zurück, der die
auf der Festplatte gewählte Tresor-Datei und das Klartext-Master-Passwort
enthält. Der Aufrufer (``app.run``) übergibt das an ``vault.store.open_vault``
und verwirft beides danach.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


@dataclass(frozen=True, slots=True)
class LoginResult:
    path: Path
    password: str


class LoginDialog(QDialog):
    """Master-Passwort-Eingabe für einen Tresor."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        default_path: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("OPN-Cockpit — Tresor entsperren")
        self.setModal(True)
        self.result_data: LoginResult | None = None

        # --- Tresor-Pfad-Auswahl ---
        self._path_edit = QLineEdit(default_path or "")
        self._path_edit.setPlaceholderText("Pfad zur .opnvault-Datei")
        browse_btn = QPushButton("Auswählen…")
        browse_btn.clicked.connect(self._pick_file)

        path_row = QHBoxLayout()
        path_row.addWidget(self._path_edit, 1)
        path_row.addWidget(browse_btn)

        # --- Master-Passwort ---
        self._pw_edit = QLineEdit()
        self._pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw_edit.setPlaceholderText("Master-Passwort (min. 12 Zeichen)")

        # --- Layout ---
        form = QFormLayout()
        form.addRow(QLabel("Tresor-Datei:"), self._build_widget_from_layout(path_row))
        form.addRow(QLabel("Passwort:"), self._pw_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Entsperren")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self._pw_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    # ----- Slots -----

    def _pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Tresor-Datei wählen",
            self._path_edit.text() or "",
            "OPN-Cockpit Tresor (*.opnvault);;Alle Dateien (*)",
        )
        if path:
            self._path_edit.setText(path)

    def _accept(self) -> None:
        path_text = self._path_edit.text().strip()
        password = self._pw_edit.text()
        if not path_text:
            return
        if not password:
            return
        self.result_data = LoginResult(path=Path(path_text), password=password)
        self.accept()

    # ----- Helper -----

    @staticmethod
    def _build_widget_from_layout(layout: QHBoxLayout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w
