"""Dialog zum Anlegen eines neuen Tresors (v1.2).

Wird vom Startup-Dialog ausgeloest, wenn der User auf "Neuen Tresor anlegen"
klickt — oder beim allerersten Start, wenn noch keine Tresor-Datei existiert.

Felder:

* Pfad — Default ``%APPDATA%/OPN-Cockpit/main.opnvault``, editierbar plus
  Browse-Button fuer Speicherort woanders.
* Master-Passwort + Bestaetigung. Erzwingt :const:`MIN_PASSWORD_LENGTH`
  und Gleichheit beider Eingaben.
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
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from opn_cockpit.vault.discovery import VAULT_EXTENSION, default_new_vault_path
from opn_cockpit.vault.store import MIN_PASSWORD_LENGTH


@dataclass(frozen=True, slots=True)
class CreateVaultResult:
    path: Path
    password: str


class CreateVaultDialog(QDialog):
    """Formular-Dialog fuer einen neuen Tresor."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        default_path: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("OPN-Cockpit — Neuen Tresor anlegen")
        self.setModal(True)
        self.resize(560, 280)
        self.result_data: CreateVaultResult | None = None

        suggested = default_path or default_new_vault_path()
        self._path_edit = QLineEdit(str(suggested))
        browse_btn = QPushButton("Speichern unter…")
        browse_btn.clicked.connect(self._pick_destination)
        path_row = QHBoxLayout()
        path_row.addWidget(self._path_edit, 1)
        path_row.addWidget(browse_btn)

        self._pw_edit = QLineEdit()
        self._pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw_edit.setPlaceholderText(
            f"Master-Passwort (min. {MIN_PASSWORD_LENGTH} Zeichen)"
        )

        self._pw_confirm = QLineEdit()
        self._pw_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw_confirm.setPlaceholderText("Passwort wiederholen")

        hint = QLabel(
            "Das Master-Passwort entschlüsselt deinen Tresor. Es lässt sich "
            "später jederzeit ändern (Datei → Master-Passwort ändern).\n"
            "Verlierst du es, gibt es keinen Weg zurück — bewahre es sicher auf."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555;")

        form = QFormLayout()
        form.addRow(QLabel("Speicherort:"), _wrap(path_row))
        form.addRow(QLabel("Master-Passwort:"), self._pw_edit)
        form.addRow(QLabel("Wiederholen:"), self._pw_confirm)
        form.addRow(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setText("Tresor anlegen")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self._pw_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    # ----- Slots -----

    def _pick_destination(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Tresor-Datei speichern unter…",
            self._path_edit.text() or "",
            f"OPN-Cockpit Tresor (*{VAULT_EXTENSION});;Alle Dateien (*)",
        )
        if path:
            if not path.endswith(VAULT_EXTENSION):
                path += VAULT_EXTENSION
            self._path_edit.setText(path)

    def _accept(self) -> None:
        path_text = self._path_edit.text().strip()
        pw1 = self._pw_edit.text()
        pw2 = self._pw_confirm.text()
        if not path_text:
            QMessageBox.warning(self, "Pfad fehlt", "Bitte einen Speicherort wählen.")
            return
        if len(pw1) < MIN_PASSWORD_LENGTH:
            QMessageBox.warning(
                self, "Passwort zu kurz",
                f"Master-Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen haben.",
            )
            return
        if pw1 != pw2:
            QMessageBox.warning(
                self, "Passwörter unterschiedlich",
                "Die beiden Passwort-Eingaben stimmen nicht überein.",
            )
            return
        path = Path(path_text)
        if path.exists():
            QMessageBox.warning(
                self, "Datei existiert",
                f"Es existiert bereits eine Datei unter:\n{path}\n\n"
                "Wähle einen anderen Speicherort oder öffne den bestehenden Tresor.",
            )
            return
        self.result_data = CreateVaultResult(path=path, password=pw1)
        self.accept()


def _wrap(layout: QHBoxLayout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    return w
