"""Tests für importers.json_aliases."""

from __future__ import annotations

import json
from pathlib import Path

from opn_cockpit.importers.json_aliases import parse_aliases_json


def _write_json(tmp_path: Path, payload: object) -> Path:
    p = tmp_path / "aliases.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


class TestHappyPath:
    def test_parses_single_entry(self, tmp_path: Path) -> None:
        path = _write_json(
            tmp_path,
            [{"name": "branch_ips", "type": "host", "content": ["1.1.1.1"]}],
        )
        result = parse_aliases_json(path)
        assert not result.has_errors
        assert len(result.specs) == 1
        assert result.specs[0].name == "branch_ips"
        assert result.specs[0].content == ("1.1.1.1",)

    def test_parses_multiple_entries(self, tmp_path: Path) -> None:
        path = _write_json(
            tmp_path,
            [
                {"name": "ips_a", "type": "host", "content": ["1.1.1.1"]},
                {"name": "ips_b", "type": "host", "content": ["2.2.2.2"]},
            ],
        )
        result = parse_aliases_json(path)
        assert len(result.specs) == 2

    def test_converts_numeric_content_to_strings(self, tmp_path: Path) -> None:
        path = _write_json(
            tmp_path,
            [{"name": "ports", "type": "port", "content": [22, 80, 443]}],
        )
        result = parse_aliases_json(path)
        assert result.specs[0].content == ("22", "80", "443")

    def test_default_merge_mode_is_create(self, tmp_path: Path) -> None:
        path = _write_json(
            tmp_path,
            [{"name": "x", "type": "host", "content": ["1.1.1.1"]}],
        )
        result = parse_aliases_json(path)
        assert result.specs[0].merge_mode == "create"

    def test_explicit_append_in_json(self, tmp_path: Path) -> None:
        path = _write_json(
            tmp_path,
            [{"name": "x", "type": "host", "content": ["1.1.1.1"], "merge_mode": "append"}],
        )
        result = parse_aliases_json(path)
        assert result.specs[0].merge_mode == "append"

    def test_override_merge_mode_wins(self, tmp_path: Path) -> None:
        path = _write_json(
            tmp_path,
            [{"name": "x", "type": "host", "content": ["1.1.1.1"], "merge_mode": "create"}],
        )
        result = parse_aliases_json(path, override_merge_mode="append")
        assert result.specs[0].merge_mode == "append"


class TestErrors:
    def test_root_must_be_list(self, tmp_path: Path) -> None:
        path = _write_json(tmp_path, {"not": "a list"})
        result = parse_aliases_json(path)
        assert result.has_errors
        assert "Liste" in result.errors[0]

    def test_invalid_name_per_entry(self, tmp_path: Path) -> None:
        path = _write_json(
            tmp_path,
            [
                {"name": "has space", "type": "host", "content": ["x"]},
                {"name": "good", "type": "host", "content": ["x"]},
            ],
        )
        result = parse_aliases_json(path)
        assert result.has_errors
        assert len(result.specs) == 1
        assert "Eintrag 1" in result.errors[0]

    def test_invalid_type_per_entry(self, tmp_path: Path) -> None:
        path = _write_json(
            tmp_path,
            [{"name": "ok", "type": "frobnicate", "content": ["x"]}],
        )
        result = parse_aliases_json(path)
        assert result.has_errors

    def test_non_list_content(self, tmp_path: Path) -> None:
        path = _write_json(
            tmp_path,
            [{"name": "ok", "type": "host", "content": "not-a-list"}],
        )
        result = parse_aliases_json(path)
        assert result.has_errors

    def test_garbage_json(self, tmp_path: Path) -> None:
        p = tmp_path / "x.json"
        p.write_text("not json", encoding="utf-8")
        result = parse_aliases_json(p)
        assert result.has_errors

    def test_missing_file(self, tmp_path: Path) -> None:
        result = parse_aliases_json(tmp_path / "no-such-file.json")
        assert result.has_errors

    def test_entry_not_a_dict(self, tmp_path: Path) -> None:
        path = _write_json(tmp_path, ["string-instead-of-dict"])
        result = parse_aliases_json(path)
        assert result.has_errors
        assert "Objekt erwartet" in result.errors[0]
