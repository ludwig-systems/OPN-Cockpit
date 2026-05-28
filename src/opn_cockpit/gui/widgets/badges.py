"""Wiederverwendbare Mini-Widgets für visuelle Hinweise.

* :func:`tls_badge` — R-SEC-4: deutlich sichtbare Markierung wenn
  TLS-Verify für ein Gerät deaktiviert ist.
* :class:`ReachabilityBadge` — v1.1-Heartbeat: kleines farbiges Pünktchen
  pro Gerät, das den letzten TCP-Probe-Status zeigt.
"""

from __future__ import annotations

from typing import Literal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QWidget

ReachabilityState = Literal["unknown", "ok", "fail", "probing"]


def tls_badge(verify_enabled: bool) -> QLabel:
    """Liefert einen kleinen QLabel-Badge: grün "TLS" oder rot "TLS AUS".

    Wird in der Inventar-Tabelle pro Gerät angezeigt — wer TLS deaktiviert
    hat, soll das auf den ersten Blick sehen (R-SEC-4).
    """
    label = QLabel("TLS" if verify_enabled else "TLS AUS")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setMinimumWidth(70)
    if verify_enabled:
        label.setStyleSheet(
            "background-color: #2e7d32; color: white; "
            "padding: 2px 6px; border-radius: 4px;"
        )
    else:
        label.setStyleSheet(
            "background-color: #c62828; color: white; font-weight: bold; "
            "padding: 2px 6px; border-radius: 4px;"
        )
        label.setToolTip(
            "TLS-Zertifikat wird NICHT geprüft — "
            "Man-in-the-Middle möglich. Risiko sichtbar machen."
        )
    return label


class ReachabilityBadge(QLabel):
    """Farbiges Pünktchen (●/○) für die TCP-Erreichbarkeit eines Geräts.

    States:
    * ``unknown`` — graues Ring-Symbol ○ (noch nicht geprobet)
    * ``probing`` — gelbes ● mit Tooltip "Pruefe..."
    * ``ok`` — grünes ● (TCP-Connect erfolgreich)
    * ``fail`` — rotes ● (TCP-Connect gescheitert / nicht erreichbar)
    """

    _STYLE_OK = "color: #2e7d32; font-size: 18pt;"
    _STYLE_FAIL = "color: #c62828; font-size: 18pt;"
    _STYLE_PROBING = "color: #e6a700; font-size: 18pt;"
    _STYLE_UNKNOWN = "color: #888; font-size: 18pt;"

    def __init__(
        self,
        host: str = "",
        port: int = 443,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumWidth(28)
        self._host = host
        self._port = port
        self._state: ReachabilityState = "unknown"
        self.set_state("unknown")

    @property
    def state(self) -> ReachabilityState:
        return self._state

    def set_state(self, state: ReachabilityState) -> None:
        self._state = state
        target = f"{self._host}:{self._port}" if self._host else "?"
        if state == "ok":
            self.setText("●")
            self.setStyleSheet(self._STYLE_OK)
            self.setToolTip(f"Erreichbar (TCP {target})")
        elif state == "fail":
            self.setText("●")
            self.setStyleSheet(self._STYLE_FAIL)
            self.setToolTip(f"Nicht erreichbar (TCP-Connect auf {target} fehlgeschlagen)")
        elif state == "probing":
            self.setText("●")
            self.setStyleSheet(self._STYLE_PROBING)
            self.setToolTip(f"Pruefe Verbindung zu {target} ...")
        else:
            self.setText("○")
            self.setStyleSheet(self._STYLE_UNKNOWN)
            self.setToolTip("Status unbekannt — noch nicht geprobet.")
