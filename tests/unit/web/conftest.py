"""Web-Test-Fixtures: schnelle Argon2-Parameter (gleicher Trick wie in vault-Tests).

Die Inventory-Routen rufen ``open_vault`` zur Passwort-Verifikation auf,
plus ``save_vault`` zum Persistieren. Production-Defaults wuerden jeden
Test ueber eine Sekunde dauern lassen.

Zusatz fuer Security-Audit #14: Vault-Pfad-Validierung. Tests legen
Vaults unter ``tmp_path`` an, der per Default nicht erlaubt waere. Die
autouse-Fixture ``_allow_tmp_vault_paths`` setzt
``OPNCOCKPIT_VAULT_DIR`` pro Test auf das tmp-Root, damit die
Validierung greift, aber nicht die Tests behindert.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from opn_cockpit.vault import crypto

TEST_TIME_COST = 1
TEST_MEMORY_COST_KIB = 8
TEST_PARALLELISM = 1


@pytest.fixture(autouse=True, scope="session")
def _fast_argon2_defaults_web() -> Iterator[None]:
    saved = (
        crypto.DEFAULT_TIME_COST,
        crypto.DEFAULT_MEMORY_COST_KIB,
        crypto.DEFAULT_PARALLELISM,
    )
    crypto.DEFAULT_TIME_COST = TEST_TIME_COST
    crypto.DEFAULT_MEMORY_COST_KIB = TEST_MEMORY_COST_KIB
    crypto.DEFAULT_PARALLELISM = TEST_PARALLELISM
    try:
        yield
    finally:
        (
            crypto.DEFAULT_TIME_COST,
            crypto.DEFAULT_MEMORY_COST_KIB,
            crypto.DEFAULT_PARALLELISM,
        ) = saved


@pytest.fixture(autouse=True)
def _allow_tmp_vault_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lockert die Vault-Pfad-Validierung (Audit #14) fuer Tests.

    Pytest's ``tmp_path`` liegt in ``$TEMP``. Wir setzen
    ``OPNCOCKPIT_VAULT_DIR`` auf das Temp-Verzeichnis, damit Test-Vaults
    unter ``tmp_path`` als gueltig akzeptiert werden. Im Production-
    Betrieb bleibt die Validierung scharf (Default-Basen sind die
    APPDATA / XDG-Pfade).
    """
    monkeypatch.setenv("OPNCOCKPIT_VAULT_DIR", str(Path(tempfile.gettempdir()).resolve()))
