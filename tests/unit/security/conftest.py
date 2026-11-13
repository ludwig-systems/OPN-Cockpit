"""Fast-Argon2 fuer Security-Tests (sonst dauern UserStore-Tests ewig)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from opn_cockpit.vault import crypto


@pytest.fixture(autouse=True, scope="session")
def _fast_argon2_security() -> Iterator[None]:
    saved = (
        crypto.DEFAULT_TIME_COST,
        crypto.DEFAULT_MEMORY_COST_KIB,
        crypto.DEFAULT_PARALLELISM,
    )
    crypto.DEFAULT_TIME_COST = 1
    crypto.DEFAULT_MEMORY_COST_KIB = 8
    crypto.DEFAULT_PARALLELISM = 1
    try:
        yield
    finally:
        (
            crypto.DEFAULT_TIME_COST,
            crypto.DEFAULT_MEMORY_COST_KIB,
            crypto.DEFAULT_PARALLELISM,
        ) = saved
