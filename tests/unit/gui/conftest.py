"""GUI-Test-Fixtures: QApplication im Offscreen-Modus.

Setzt ``QT_QPA_PLATFORM=offscreen``, damit die Tests auf Headless-CI
(GitHub Actions, etc.) ohne Display laufen. Auf Windows funktioniert das
ebenfalls — Qt rendert in einen Speicherpuffer.
"""

from __future__ import annotations

import os

# Vor dem PySide6-Import setzen!
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp() -> Iterator[QApplication]:
    """Stellt sicher, dass genau eine QApplication für die Test-Session existiert."""
    app = QApplication.instance() or QApplication([])
    yield app
    # QApplication wird nicht explizit beendet — Process-Ende reicht.
