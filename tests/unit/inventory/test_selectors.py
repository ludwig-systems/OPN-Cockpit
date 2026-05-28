"""Tests für inventory.selectors — Selektor-Sprache."""

from __future__ import annotations

import pytest

from opn_cockpit.inventory.model import Device
from opn_cockpit.inventory.selectors import SelectorError, apply_selector


def _dev(name: str, *, id: str | None = None, tags: tuple[str, ...] = ()) -> Device:
    return Device(
        id=id or f"id-{name.lower()}",
        name=name,
        host=f"{name.lower()}.lab",
        port=443,
        tls_verify=True,
        tags=tags,
        descr="",
    )


@pytest.fixture()
def devices() -> list[Device]:
    return [
        _dev("Berlin", tags=("branches", "germany")),
        _dev("München", tags=("branches", "germany")),
        _dev("HQ-Core", tags=("core",)),
        _dev("Backup-Box", id="custom-uuid-123"),
    ]


class TestAll:
    def test_empty_selector_returns_all(self, devices: list[Device]) -> None:
        assert apply_selector(devices, "") == devices

    def test_all_keyword(self, devices: list[Device]) -> None:
        assert apply_selector(devices, "all") == devices

    def test_all_case_insensitive(self, devices: list[Device]) -> None:
        assert apply_selector(devices, "ALL") == devices

    def test_whitespace_is_stripped(self, devices: list[Device]) -> None:
        assert apply_selector(devices, "   all   ") == devices


class TestTagAndGroup:
    def test_tag_filter(self, devices: list[Device]) -> None:
        result = apply_selector(devices, "tag:branches")
        assert {d.name for d in result} == {"Berlin", "München"}

    def test_group_is_alias_for_tag(self, devices: list[Device]) -> None:
        result = apply_selector(devices, "group:germany")
        assert {d.name for d in result} == {"Berlin", "München"}

    def test_tag_case_insensitive(self, devices: list[Device]) -> None:
        result = apply_selector(devices, "tag:BRANCHES")
        assert len(result) == 2


class TestId:
    def test_id_exact_match(self, devices: list[Device]) -> None:
        result = apply_selector(devices, "id:custom-uuid-123")
        assert len(result) == 1
        assert result[0].name == "Backup-Box"

    def test_id_no_match(self, devices: list[Device]) -> None:
        assert apply_selector(devices, "id:does-not-exist") == []


class TestName:
    def test_name_partial_match(self, devices: list[Device]) -> None:
        result = apply_selector(devices, "name:berlin")
        assert len(result) == 1
        assert result[0].name == "Berlin"

    def test_bare_term_treated_as_name(self, devices: list[Device]) -> None:
        result = apply_selector(devices, "hq")
        assert result[0].name == "HQ-Core"


class TestUnion:
    def test_multiple_selectors_union(self, devices: list[Device]) -> None:
        result = apply_selector(devices, "tag:core, tag:branches")
        assert {d.name for d in result} == {"Berlin", "München", "HQ-Core"}

    def test_duplicates_removed(self, devices: list[Device]) -> None:
        result = apply_selector(devices, "tag:branches, tag:germany")
        # beide Tags treffen Berlin und München — sollen aber nur je einmal vorkommen
        names = [d.name for d in result]
        assert names == ["Berlin", "München"]


class TestErrors:
    def test_unknown_kind_raises(self, devices: list[Device]) -> None:
        with pytest.raises(SelectorError):
            apply_selector(devices, "kind:unknown")

    def test_empty_value_raises(self, devices: list[Device]) -> None:
        with pytest.raises(SelectorError):
            apply_selector(devices, "tag:")
