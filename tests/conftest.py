"""Globale Pytest-Fixtures für OPN-Cockpit.

Verbindlich (siehe Plan, Schritt 4 & 8):

* ``keyring``-Backend wird über die Fixture ``in_memory_keyring`` injiziert,
  damit Tests nicht das System-Backend des CI/Dev-Hosts berühren.
* Argon2-Kosten werden in Tests künstlich niedrig gehalten (eigene Fixture
  in ``tests/unit/security/conftest.py``, nicht im Production-Code).
* Inventory schreibt nach ``tmp_path``.

Diese conftest enthält absichtlich nur Pakete-übergreifende, harmlose Fixtures.
Spezialisiertere Fixtures gehören in untergeordnete ``conftest.py``-Dateien.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture()
def appdata_dir(tmp_path: Path) -> Iterator[Path]:
    """Isoliertes Daten-Verzeichnis je Test (Ersatz für %APPDATA%\\OPN-Cockpit)."""
    target = tmp_path / "OPN-Cockpit"
    target.mkdir()
    yield target
