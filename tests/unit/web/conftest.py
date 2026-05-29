"""Web-Test-Fixtures: schnelle Argon2-Parameter (gleicher Trick wie in vault-Tests).

Die Inventory-Routen rufen ``open_vault`` zur Passwort-Verifikation auf,
plus ``save_vault`` zum Persistieren. Production-Defaults wuerden jeden
Test ueber eine Sekunde dauern lassen.
"""

from __future__ import annotations

from collections.abc import Iterator

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
