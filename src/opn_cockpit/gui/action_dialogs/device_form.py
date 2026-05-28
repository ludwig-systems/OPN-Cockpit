"""Add-Device-Dialog: Stammdaten + API-Key/Secret eines neuen Geräts."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


@dataclass(frozen=True, slots=True)
class DeviceFormResult:
    name: str
    host: str
    port: int
    tls_verify: bool
    tags: list[str]
    descr: str
    api_key: str
    api_secret: str


class DeviceFormDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Gerät hinzufügen")
        self.setModal(True)
        self.resize(500, 360)

        self._name = QLineEdit()
        self._host = QLineEdit()
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(443)
        self._tls = QCheckBox("TLS-Zertifikat prüfen")
        self._tls.setChecked(True)
        self._tags = QLineEdit()
        self._tags.setPlaceholderText("Komma-separiert, z. B. branches,germany")
        self._descr = QLineEdit()
        self._api_key = QLineEdit()
        self._api_secret = QLineEdit()
        self._api_secret.setEchoMode(QLineEdit.EchoMode.Password)

        form = QFormLayout()
        form.addRow("Name:", self._name)
        form.addRow("Host (IP oder FQDN):", self._host)
        form.addRow("Port:", self._port)
        form.addRow("TLS:", self._tls)
        form.addRow("Tags:", self._tags)
        form.addRow("Beschreibung:", self._descr)
        form.addRow("API-Key:", self._api_key)
        form.addRow("API-Secret:", self._api_secret)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def result_data(self) -> DeviceFormResult | None:
        name = self._name.text().strip()
        host = self._host.text().strip()
        if not name or not host:
            return None
        tags = [t.strip() for t in self._tags.text().split(",") if t.strip()]
        return DeviceFormResult(
            name=name,
            host=host,
            port=self._port.value(),
            tls_verify=self._tls.isChecked(),
            tags=tags,
            descr=self._descr.text().strip(),
            api_key=self._api_key.text(),
            api_secret=self._api_secret.text(),
        )
