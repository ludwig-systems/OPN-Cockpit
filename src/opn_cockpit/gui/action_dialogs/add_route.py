"""Add-Route-Dialog: Eingabe einer neuen Route + Selektor."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from opn_cockpit.core.objects.routes import RouteSpec


@dataclass(frozen=True, slots=True)
class AddRouteResult:
    spec: RouteSpec
    selector: str


class AddRouteDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        default_selector: str = "all",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Route hinzufügen — Plan erzeugen")
        self.setModal(True)
        self.resize(500, 280)

        self._network = QLineEdit()
        self._network.setPlaceholderText("z. B. 10.99.0.0/24")
        self._gateway = QLineEdit()
        self._gateway.setPlaceholderText("z. B. V2_WANBwIn (case-sensitive)")
        self._descr = QLineEdit()
        self._disabled = QCheckBox("Route deaktiviert anlegen")
        self._selector = QLineEdit(default_selector)
        self._selector.setPlaceholderText("all, tag:X, group:X, id:X, name:X")

        form = QFormLayout()
        form.addRow("Zielnetz (CIDR):", self._network)
        form.addRow("Gateway-Name:", self._gateway)
        form.addRow("Beschreibung:", self._descr)
        form.addRow("Disabled:", self._disabled)
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

    def result_data(self) -> AddRouteResult | None:
        network = self._network.text().strip()
        gateway = self._gateway.text().strip()
        if not network or not gateway:
            return None
        spec = RouteSpec(
            network=network,
            gateway=gateway,
            descr=self._descr.text().strip(),
            disabled=self._disabled.isChecked(),
        )
        return AddRouteResult(
            spec=spec,
            selector=self._selector.text().strip() or "all",
        )
