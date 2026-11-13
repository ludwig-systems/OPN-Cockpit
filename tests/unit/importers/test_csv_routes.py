"""Tests für importers.csv_routes."""

from __future__ import annotations

from pathlib import Path

from opn_cockpit.importers.csv_routes import parse_routes_csv


def _write_csv(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "routes.csv"
    p.write_text(content, encoding="utf-8")
    return p


class TestHappyPath:
    def test_parses_minimal_csv(self, tmp_path: Path) -> None:
        path = _write_csv(
            tmp_path,
            "network,gateway\n10.0.0.0/24,WAN_GW\n",
        )
        result = parse_routes_csv(path)
        assert not result.has_errors
        assert len(result.specs) == 1
        assert result.specs[0].network == "10.0.0.0/24"

    def test_parses_full_columns(self, tmp_path: Path) -> None:
        path = _write_csv(
            tmp_path,
            "network,gateway,descr,disabled\n"
            "10.0.0.0/24,WAN_GW,Office,1\n"
            "10.1.0.0/24,WAN_GW,Lab,0\n",
        )
        result = parse_routes_csv(path)
        assert len(result.specs) == 2
        assert result.specs[0].disabled is True
        assert result.specs[1].disabled is False
        assert result.specs[0].descr == "Office"

    def test_skips_blank_lines_and_comments(self, tmp_path: Path) -> None:
        path = _write_csv(
            tmp_path,
            "network,gateway\n"
            "# erste Kommentarzeile\n"
            "\n"
            "10.0.0.0/24,WAN_GW\n"
            "# noch ein Kommentar\n"
            "10.1.0.0/24,WAN_GW\n",
        )
        result = parse_routes_csv(path)
        assert len(result.specs) == 2

    def test_disabled_truthy_variants(self, tmp_path: Path) -> None:
        path = _write_csv(
            tmp_path,
            "network,gateway,disabled\n"
            "10.0.0.0/24,WAN_GW,ja\n"
            "10.1.0.0/24,WAN_GW,true\n"
            "10.2.0.0/24,WAN_GW,0\n",
        )
        result = parse_routes_csv(path)
        assert [s.disabled for s in result.specs] == [True, True, False]


class TestErrors:
    def test_missing_required_column(self, tmp_path: Path) -> None:
        path = _write_csv(tmp_path, "network,wrong\n10.0/8,xy\n")
        result = parse_routes_csv(path)
        assert result.has_errors
        assert "gateway" in result.errors[0]

    def test_invalid_cidr(self, tmp_path: Path) -> None:
        path = _write_csv(
            tmp_path,
            "network,gateway\nnot-a-cidr,WAN_GW\n10.0.0.0/24,WAN_GW\n",
        )
        result = parse_routes_csv(path)
        assert result.has_errors
        assert len(result.specs) == 1  # andere Zeile geht durch
        assert "Zeile 2" in result.errors[0]

    def test_invalid_gateway_name(self, tmp_path: Path) -> None:
        path = _write_csv(
            tmp_path,
            "network,gateway\n10.0.0.0/24,has space\n",
        )
        result = parse_routes_csv(path)
        assert result.has_errors

    def test_missing_file(self, tmp_path: Path) -> None:
        result = parse_routes_csv(tmp_path / "no-such-file.csv")
        assert result.has_errors
        assert "nicht lesbar" in result.errors[0]

    def test_empty_file(self, tmp_path: Path) -> None:
        path = _write_csv(tmp_path, "")
        result = parse_routes_csv(path)
        assert result.has_errors
