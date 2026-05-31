"""Tests fuer parse_version + compare_versions."""

from __future__ import annotations

import pytest

from opn_cockpit.updates.version import compare_versions, parse_version


class TestParseVersion:
    @pytest.mark.parametrize("raw,expected", [
        ("0.6.0", (0, 6, 0, False)),
        ("v0.6.0", (0, 6, 0, False)),
        ("V0.6.0", (0, 6, 0, False)),
        ("  0.6.0  ", (0, 6, 0, False)),
        ("1.2.3", (1, 2, 3, False)),
        ("0.7.0rc1", (0, 7, 0, True)),
        ("0.7.0.dev0", (0, 7, 0, True)),
        ("v0.7.0-beta.2", (0, 7, 0, True)),
        ("0.7.0a1", (0, 7, 0, True)),
    ])
    def test_parses_valid_versions(
        self, raw: str, expected: tuple[int, int, int, bool],
    ) -> None:
        assert parse_version(raw) == expected

    @pytest.mark.parametrize("raw", [
        "",
        "v",
        "1.2",
        "abc",
        "1.2.x",
        None,
    ])
    def test_returns_none_for_unparseable(self, raw: str | None) -> None:
        assert parse_version(raw or "") is None


class TestCompareVersions:
    @pytest.mark.parametrize("left,right,expected", [
        ("0.6.0", "0.7.0", -1),
        ("0.7.0", "0.6.0", 1),
        ("0.6.0", "0.6.0", 0),
        ("v0.6.0", "0.6.0", 0),
        ("0.6.0", "0.6.1", -1),
        ("0.6.10", "0.6.9", 1),
        ("1.0.0", "0.99.99", 1),
        # Pre-release verliert gegen Release derselben Base.
        ("0.7.0rc1", "0.7.0", -1),
        ("0.7.0", "0.7.0rc1", 1),
        # Pre-release < hoehere Release.
        ("0.6.0rc1", "0.7.0", -1),
    ])
    def test_ordering(self, left: str, right: str, expected: int) -> None:
        assert compare_versions(left, right) == expected

    @pytest.mark.parametrize("left,right", [
        ("", "0.6.0"),
        ("0.6.0", ""),
        ("kaputt", "0.6.0"),
        ("0.6.0", "kaputt"),
    ])
    def test_unparseable_returns_zero(self, left: str, right: str) -> None:
        # Defensive: kein false-positive Update-Banner durch kaputten Tag.
        assert compare_versions(left, right) == 0
