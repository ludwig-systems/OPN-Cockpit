"""Add/Append-Alias-Dialog: Eingabe eines Aliases + Merge-Modus + Selektor."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from opn_cockpit.core.objects.aliases import AliasSpec
from opn_cockpit.core.validation import ALLOWED_ALIAS_TYPES


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
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Alias hinzufügen / erweitern — Plan erzeugen")
        self.setModal(True)
        self.resize(560, 460)

        self._name = QLineEdit()
        self._name.setPlaceholderText("z. B. branch_ips")
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

        form = QFormLayout()
        form.addRow("Name:", self._name)
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
        name = self._name.text().strip()
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


def _parse_content_lines(raw: str) -> list[str]:
    """Akzeptiert Newline- ODER Komma-separierte Einträge."""
    items: list[str] = []
    for chunk in raw.replace(",", "\n").splitlines():
        stripped = chunk.strip()
        if stripped:
            items.append(stripped)
    return items
