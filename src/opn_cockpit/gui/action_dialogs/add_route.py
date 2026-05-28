"""Add-Route-Dialog: Eingabe einer neuen Route + Selektor.

v1.1: Wenn der Aufrufer einen ``gateway_suggestions``-Callback injiziert
und Geräte uebergibt, kann der User per Klick die vorhandenen
Gateway-Namen vom ausgewaehlten Referenz-Geraet laden — Combo statt
Freitext, um Tippfehler bei case-sensitiven Namen zu vermeiden.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from opn_cockpit.core.objects.routes import RouteSpec
from opn_cockpit.inventory.model import Device


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
        devices: list[Device] | None = None,
        gateway_suggestions: Callable[[Device], list[str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Route hinzufügen — Plan erzeugen")
        self.setModal(True)
        self.resize(560, 320)

        self._devices = list(devices or [])
        self._gateway_suggestions = gateway_suggestions

        self._network = QLineEdit()
        self._network.setPlaceholderText("z. B. 10.99.0.0/24")
        self._gateway = QComboBox()
        self._gateway.setEditable(True)
        gateway_edit = self._gateway.lineEdit()
        if gateway_edit is not None:
            gateway_edit.setPlaceholderText("z. B. V2_WANBwIn (case-sensitive)")
        self._descr = QLineEdit()
        self._disabled = QCheckBox("Route deaktiviert anlegen")
        self._selector = QLineEdit(default_selector)
        self._selector.setPlaceholderText("all, tag:X, group:X, id:X, name:X")

        # --- Vorschlagsleiste (nur sichtbar, wenn discover-Callback existiert) ---
        suggest_row: QHBoxLayout | None = None
        self._reference_device: QComboBox | None = None
        if self._gateway_suggestions is not None and self._devices:
            self._reference_device = QComboBox()
            for device in self._devices:
                self._reference_device.addItem(device.display_label, device)
            load_btn = QPushButton("Gateways laden")
            load_btn.clicked.connect(self._load_gateways)
            suggest_row = QHBoxLayout()
            suggest_row.addWidget(self._reference_device, 1)
            suggest_row.addWidget(load_btn)

        form = QFormLayout()
        form.addRow("Zielnetz (CIDR):", self._network)
        form.addRow("Gateway-Name:", self._gateway)
        if suggest_row is not None:
            form.addRow("Vorschläge von:", _wrap(suggest_row))
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
        gateway = self._gateway.currentText().strip()
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

    # ----- Discovery -----

    def _load_gateways(self) -> None:
        if self._reference_device is None or self._gateway_suggestions is None:
            return
        device = self._reference_device.currentData()
        if not isinstance(device, Device):
            return
        try:
            names = self._gateway_suggestions(device)
        except Exception as exc:
            QMessageBox.warning(
                self, "Gateway-Liste",
                f"Konnte Gateway-Liste nicht abrufen:\n{exc}",
            )
            return
        if not names:
            QMessageBox.information(
                self, "Gateway-Liste",
                f"{device.name} hat keine Gateways gemeldet "
                "(oder der Endpoint hat ein anderes Format).",
            )
            return
        current = self._gateway.currentText()
        self._gateway.clear()
        self._gateway.addItems(names)
        if current and current not in names:
            self._gateway.setEditText(current)


def _wrap(layout: QHBoxLayout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    return w
