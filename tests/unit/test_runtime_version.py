"""Tests fuer runtime_version: _strip_v_prefix + get_runtime_version cached + clean."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from opn_cockpit.runtime_version import _strip_v_prefix, get_runtime_version


class TestStripVPrefix:
    @pytest.mark.parametrize(
        "inp,expected",
        [
            ("v0.7.0", "0.7.0"),
            ("V0.7.0", "0.7.0"),
            ("0.7.0", "0.7.0"),
            ("v1.2.3-rc1", "1.2.3-rc1"),
            ("", ""),
            ("vvvv", "vvv"),  # nur das erste v
        ],
    )
    def test_strips_leading_v(self, inp: str, expected: str) -> None:
        assert _strip_v_prefix(inp) == expected


class TestGetRuntimeVersion:
    def setup_method(self) -> None:
        # Cache zwischen Tests leeren - get_runtime_version ist lru_cached
        get_runtime_version.cache_clear()

    def teardown_method(self) -> None:
        get_runtime_version.cache_clear()

    def test_git_tag_with_v_prefix_stripped(self) -> None:
        """Wichtigster Test: Tag 'v0.7.0' wird zu '0.7.0' bereinigt.

        Sonst rendert das Template 'v{{ version }}' als 'vv0.7.0'.
        """
        with (
            patch(
                "opn_cockpit.runtime_version._git_describe_tag",
                return_value="v0.7.0",
            ),
            patch(
                "opn_cockpit.runtime_version._package_repo_dir",
                return_value=__file__,  # any non-None Path-like value
            ),
        ):
            assert get_runtime_version() == "0.7.0"

    def test_no_git_falls_back_to_version_constant(self) -> None:
        with (
            patch(
                "opn_cockpit.runtime_version._package_repo_dir",
                return_value=None,
            ),
            patch(
                "opn_cockpit.runtime_version.__version__",
                "1.2.3.dev0",
            ),
        ):
            assert get_runtime_version() == "1.2.3.dev0"

    def test_version_constant_v_prefix_also_stripped(self) -> None:
        """Defensive: auch __version__ wird durch den Strip-Filter geschickt."""
        with (
            patch(
                "opn_cockpit.runtime_version._package_repo_dir",
                return_value=None,
            ),
            patch(
                "opn_cockpit.runtime_version.__version__",
                "v0.7.0",  # untypisch aber moeglich
            ),
        ):
            assert get_runtime_version() == "0.7.0"
