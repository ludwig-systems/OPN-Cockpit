"""Add/Append-Alias-Dialog mit optionaler Auto-Complete-Liste (v1.1).

Beim Append-Modus moechte man typischerweise an einen BESTEHENDEN Alias
anhaengen. Damit der Name nicht falsch geschrieben wird, kann der Aufrufer
einen ``alias_suggestions``-Callback injizieren. Bei Klick auf
"Aliasse laden" zeigt der Combo-Box die Namen der ausgewaehlten Referenz-
Maschine.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from opn_cockpit.core.objects.aliases import AliasSpec
from opn_cockpit.core.validation import ALLOWED_ALIAS_TYPES
from opn_cockpit.inventory.model import Device


@dataclass(frozen=True, slots=True)
class AddAliasResult:
    spec: AliasSpec
    selector: str


class AddAliasDialog(QDialog):
    """Vereint create + append. Modus wird per Radio-Button gewählt."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        default_selector: str = "all",
        devices: list[Device] | None = None,
        alias_suggestions: Callable[[Device], list[str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Alias hinzufügen / erweitern — Plan erzeugen")
        self.setModal(True)
        self.resize(620, 520)

        self._devices = list(devices or [])
        self._alias_suggestions = alias_suggestions

        self._name = QComboBox()
        self._name.setEditable(True)
        name_edit = self._name.lineEdit()
        if name_edit is not None:
            name_edit.setPlaceholderText("z. B. branch_ips")
        self._type = QComboBox()
        for t in sorted(ALLOWED_ALIAS_TYPES):
            self._type.addItem(t)
        self._content = QPlainTextEdit()
        self._content.setPlaceholderText(
            "Ein Eintrag pro Zeile (oder Komma-separiert).\nBeispiel:\n10.0.0.1\n10.0.0.2"
        )
        self._descr = QLineEdit()

        self._mode_create = QRadioButton("Neu anlegen (create)")
        self._mode_create.setChecked(True)
        self._mode_append = QRadioButton("An bestehenden Alias anhängen (append/merge)")

        self._selector = QLineEdit(default_selector)
        self._selector.setPlaceholderText("all, tag:X, group:X, id:X, name:X")

        # --- Vorschlagsleiste fuer existierende Alias-Namen ---
        suggest_row: QHBoxLayout | None = None
        self._reference_device: QComboBox | None = None
        if self._alias_suggestions is not None and self._devices:
            self._reference_device = QComboBox()
            for device in self._devices:
                self._reference_device.addItem(device.display_label, device)
            load_btn = QPushButton("Aliasse laden")
            load_btn.clicked.connect(self._load_aliases)
            suggest_row = QHBoxLayout()
            suggest_row.addWidget(self._reference_device, 1)
            suggest_row.addWidget(load_btn)

        form = QFormLayout()
        form.addRow("Name:", self._name)
        if suggest_row is not None:
            form.addRow("Vorschläge von:", _wrap(suggest_row))
        form.addRow("Typ:", self._type)
        form.addRow("Inhalt:", self._content)
        form.addRow("Beschreibung:", self._descr)
        form.addRow("Modus:", self._mode_create)
        form.addRow("", self._mode_append)
        form.addRow("Geräte-Auswahl:", self._selector)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setText("Plan erzeugen")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def result_data(self) -> AddAliasResult | None:
        name = self._name.currentText().strip()
        type_ = self._type.currentText()
        raw = self._content.toPlainText()
        if not name or not raw.strip():
            return None
        entries = _parse_content_lines(raw)
        if not entries:
            return None
        spec = AliasSpec(
            name=name,
            type=type_,
            content=tuple(entries),
            descr=self._descr.text().strip(),
            merge_mode="append" if self._mode_append.isChecked() else "create",
        )
        return AddAliasResult(
            spec=spec,
            selector=self._selector.text().strip() or "all",
        )

    # ----- Discovery -----

    def _load_aliases(self) -> None:
        if self._reference_device is None or self._alias_suggestions is None:
            return
        device = self._reference_device.currentData()
        if not isinstance(device, Device):
            return
        try:
            names = self._alias_suggestions(device)
        except Exception as exc:
            QMessageBox.warning(
                self, "Alias-Liste",
                f"Konnte Alias-Liste nicht abrufen:\n{exc}",
            )
            return
        if not names:
            QMessageBox.information(
                self, "Alias-Liste",
                f"{device.name} hat keine Aliasse gemeldet "
                "(oder der Endpoint hat ein anderes Format).",
            )
            return
        current = self._name.currentText()
        self._name.clear()
        self._name.addItems(names)
        if current and current not in names:
            self._name.setEditText(current)


def _parse_content_lines(raw: str) -> list[str]:
    """Akzeptiert Newline- ODER Komma-separierte Einträge."""
    items: list[str] = []
    for chunk in raw.replace(",", "\n").splitlines():
        stripped = chunk.strip()
        if stripped:
            items.append(stripped)
    return items


def _wrap(layout: QHBoxLayout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    return w
