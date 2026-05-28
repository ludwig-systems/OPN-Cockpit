"""Wiederverwendbare Mini-Widgets für visuelle Hinweise (R-SEC-4)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel


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
