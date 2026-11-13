"""Vault-Test-Fixtures: schnelle Argon2-Parameter für sub-Sekunden-Testläufe.

Production-Defaults (``time_cost=4``, ``memory_cost=256 MiB``) brauchen pro
Tresor-Operation 1-2 Sekunden. Über 50 Vault-Tests würden so >1 min.
Stattdessen monkeypatchen wir die Defaults für die gesamte Session-Dauer
in der Test-Suite — das echte Production-Tuning wird **nicht** angefasst,
weil der Patch nur in den Test-Imports greift.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from opn_cockpit.vault import crypto

# Argon2id mit minimalen Kosten — kryptographisch nicht relevant, weil
# wir keine echten Geheimnisse halten, sondern Funktionalität testen.
TEST_TIME_COST = 1
TEST_MEMORY_COST_KIB = 8  # 8 KiB Minimum
TEST_PARALLELISM = 1


@pytest.fixture(autouse=True, scope="session")
def _fast_argon2_defaults() -> Iterator[None]:
    """Senkt KDF-Kosten für die gesamte Test-Session."""
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


@pytest.fixture()
def valid_password() -> str:
    return "korrektes-pferd-batterie-heftklammer"


@pytest.fixture()
def vault_path(tmp_path: object) -> object:
    # Lokal benannter Path, damit Tests klar lesbar bleiben.
    return tmp_path / "test.opnvault"  # type: ignore[operator]
