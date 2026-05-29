"""Startup-Dialog: Tresor-Auswahl + Master-Passwort (v1.2).

Boot-Flow ist GUI-only. Beim Start scannt
:func:`opn_cockpit.vault.discovery.discover_vaults` das App-Daten-Verzeichnis
plus Recent-Liste und liefert eine Auswahl. Je nach Treffer-Anzahl verhaelt
sich der Dialog unterschiedlich:

* **0 Tresore** — Hinweis-Text, Combo deaktiviert. Der User sieht den Button
  "Neuen Tresor anlegen..." und kann ueber "Andere Datei..." auch einen
  bestehenden, woanders gespeicherten Tresor oeffnen.
* **1 Tresor** — Combo zeigt diesen einen Tresor vorausgewaehlt, Passwort-Feld
  hat Fokus. Ein Tastendruck reicht zum Entsperren.
* **>1 Tresore** — Combo listet alle, der Default ist vorausgewaehlt.

Liefert ein ``LoginResult`` zurueck, falls der User mit gefuelltem Passwort
auf "Entsperren" klickt; sonst ``None``. Wenn ein neuer Tresor angelegt
wurde, ist zusätzlich ``created_vault`` gesetzt — der Aufrufer triggert
dann ``vault.store.create_vault``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
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

from opn_cockpit.gui.create_vault_dialog import CreateVaultDialog, CreateVaultResult
from opn_cockpit.vault.discovery import VAULT_EXTENSION


@dataclass(frozen=True, slots=True)
class LoginResult:
    path: Path
    password: str


class LoginDialog(QDialog):
    """Vault-Picker + Passwort-Eingabe in einem Dialog."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        available_vaults: list[Path] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("OPN-Cockpit")
        self.setModal(True)
        self.resize(640, 260)
        self.result_data: LoginResult | None = None
        self._created_vault: CreateVaultResult | None = None

        # --- Tresor-Auswahl ---
        self._vault_combo = QComboBox()
        self._vault_combo.setMinimumWidth(380)
        browse_btn = QPushButton("Andere Datei…")
        browse_btn.clicked.connect(self._pick_other)
        create_btn = QPushButton("Neuen Tresor anlegen…")
        create_btn.clicked.connect(self._create_new)

        picker_row = QHBoxLayout()
        picker_row.addWidget(self._vault_combo, 1)
        picker_row.addWidget(browse_btn)

        # --- Master-Passwort ---
        self._pw_edit = QLineEdit()
        self._pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw_edit.setPlaceholderText("Master-Passwort")
        self._pw_edit.returnPressed.connect(self._accept)

        # --- Hinweis ---
        self._info_label = QLabel()
        self._info_label.setWordWrap(True)
        self._info_label.setStyleSheet("color: #444;")

        # --- Layout ---
        form = QFormLayout()
        form.addRow(QLabel("Tresor-Datei:"), _wrap(picker_row))
        form.addRow(QLabel("Passwort:"), self._pw_edit)

        bottom_row = QHBoxLayout()
        bottom_row.addWidget(create_btn)
        bottom_row.addStretch()

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setText("Entsperren")
        self._ok_button = ok
        self._buttons.accepted.connect(self._accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._info_label)
        layout.addLayout(bottom_row)
        layout.addWidget(self._buttons)

        self._populate(available_vaults or [])

    # ----- Public -----

    @property
    def created_vault(self) -> CreateVaultResult | None:
        """Falls der User waehrend des Dialogs einen neuen Tresor angelegt hat,
        liefert dies den frischen ``CreateVaultResult`` zurueck — sonst
        ``None``. Der Aufrufer kann dann ``vault.store.create_vault``
        anstossen."""
        return self._created_vault

    # ----- Internals -----

    def _populate(self, vaults: list[Path]) -> None:
        self._vault_combo.clear()
        for p in vaults:
            self._vault_combo.addItem(_format_vault_label(p), p)

        if not vaults:
            self._info_label.setText(
                "Es wurde noch kein Tresor gefunden. Klicke "
                "<b>Neuen Tresor anlegen…</b> für den ersten Start oder "
                "<b>Andere Datei…</b>, um einen vorhandenen Tresor zu öffnen."
            )
            self._pw_edit.setEnabled(False)
            if self._ok_button is not None:
                self._ok_button.setEnabled(False)
            self._vault_combo.setEnabled(False)
            return

        count = len(vaults)
        if count == 1:
            self._info_label.setText(
                "Ein Tresor gefunden — bitte Passwort eingeben."
            )
        else:
            self._info_label.setText(
                f"{count} Tresore gefunden — bitte einen auswählen und "
                "Passwort eingeben."
            )
        self._pw_edit.setEnabled(True)
        if self._ok_button is not None:
            self._ok_button.setEnabled(True)
        self._vault_combo.setEnabled(True)
        self._vault_combo.setCurrentIndex(0)
        self._pw_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    # ----- Slots -----

    def _pick_other(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Tresor-Datei wählen",
            "",
            f"OPN-Cockpit Tresor (*{VAULT_EXTENSION});;Alle Dateien (*)",
        )
        if not path:
            return
        p = Path(path)
        # Vorhanden? Combo darauf positionieren.
        for idx in range(self._vault_combo.count()):
            existing = self._vault_combo.itemData(idx)
            if isinstance(existing, Path) and _same(existing, p):
                self._vault_combo.setCurrentIndex(idx)
                self._activate_inputs()
                return
        self._vault_combo.addItem(_format_vault_label(p), p)
        self._vault_combo.setCurrentIndex(self._vault_combo.count() - 1)
        self._activate_inputs()

    def _create_new(self) -> None:
        dlg = CreateVaultDialog(self)
        if dlg.exec() != CreateVaultDialog.DialogCode.Accepted:
            return
        created = dlg.result_data
        if created is None:
            return
        self._created_vault = created
        # Die tatsaechliche Anlage uebernimmt der Aufrufer (gui/app.py); wir
        # tragen den Pfad und das Passwort als Result ein und schliessen.
        self.result_data = LoginResult(
            path=created.path, password=created.password
        )
        self.accept()

    def _activate_inputs(self) -> None:
        self._pw_edit.setEnabled(True)
        if self._ok_button is not None:
            self._ok_button.setEnabled(True)
        self._vault_combo.setEnabled(True)
        self._info_label.setText("")
        self._pw_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _accept(self) -> None:
        if self._ok_button is None or not self._ok_button.isEnabled():
            return
        path = self._vault_combo.currentData()
        password = self._pw_edit.text()
        if not isinstance(path, Path) or not password:
            return
        self.result_data = LoginResult(path=path, password=password)
        self.accept()


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _wrap(layout: QHBoxLayout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    return w


def _format_vault_label(p: Path) -> str:
    """Zeigt Dateiname + Verzeichnis als zweiteilige Info."""
    parent = str(p.parent)
    return f"{p.name}    —   {parent}"


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)
