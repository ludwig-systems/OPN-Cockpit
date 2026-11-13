"""Tests fuer config_compare.extract_aliases + compare_aliases."""

from __future__ import annotations

from opn_cockpit.core.config_compare import (
    AliasItem,
    compare_aliases,
    extract_aliases,
)

# OPNsense 24.x+-Struktur (verschachtelt unter OPNsense/Firewall/Alias)
NEW_OPNSENSE_XML = b"""<?xml version="1.0"?>
<opnsense>
  <OPNsense>
    <Firewall>
      <Alias version="1.0.1">
        <aliases>
          <alias uuid="aaa">
            <enabled>1</enabled>
            <name>WHITELIST_OUR</name>
            <type>network</type>
            <content>10.0.0.0/24
10.0.1.0/24</content>
            <description>Our networks</description>
          </alias>
          <alias uuid="bbb">
            <enabled>1</enabled>
            <name>BLOCKED_PORTS</name>
            <type>port</type>
            <content>23,135,445</content>
            <description>Default block</description>
          </alias>
        </aliases>
      </Alias>
    </Firewall>
  </OPNsense>
</opnsense>
"""

# Aelteres Schema (Aliase top-level)
OLD_OPNSENSE_XML = b"""<?xml version="1.0"?>
<opnsense>
  <aliases>
    <alias>
      <name>WHITELIST_OUR</name>
      <type>network</type>
      <content>10.0.0.0/24 10.0.1.0/24</content>
      <descr>Our networks legacy</descr>
    </alias>
  </aliases>
</opnsense>
"""


class TestExtractAliases:
    def test_modern_schema(self) -> None:
        items = extract_aliases(NEW_OPNSENSE_XML)
        names = sorted(i.name for i in items)
        assert names == ["BLOCKED_PORTS", "WHITELIST_OUR"]
        wh = next(i for i in items if i.name == "WHITELIST_OUR")
        assert wh.type == "network"
        # Content sortiert (Reihenfolge irrelevant fuer Mengen)
        assert wh.content == ("10.0.0.0/24", "10.0.1.0/24")
        assert wh.description == "Our networks"

    def test_legacy_schema(self) -> None:
        items = extract_aliases(OLD_OPNSENSE_XML)
        assert len(items) == 1
        assert items[0].name == "WHITELIST_OUR"
        # description aus dem alten <descr>-Element
        assert items[0].description == "Our networks legacy"

    def test_invalid_xml_returns_empty(self) -> None:
        assert extract_aliases(b"not <valid xml >>") == []

    def test_content_split_by_comma(self) -> None:
        items = extract_aliases(NEW_OPNSENSE_XML)
        ports = next(i for i in items if i.name == "BLOCKED_PORTS")
        assert ports.content == ("135", "23", "445")  # sortiert


class TestCompareAliases:
    def _items(self, *names_with_content: tuple[str, str, str]) -> list[AliasItem]:
        return [
            AliasItem(name=n, type=t, content=tuple(c.split()), description="")
            for n, t, c in names_with_content
        ]

    def test_all_identical_uniform(self) -> None:
        same = self._items(("ALIAS_A", "network", "10.0.0.0/24"))
        result = compare_aliases(
            {"dev1": same, "dev2": same, "dev3": same},
            columns=["dev1", "dev2", "dev3"],
        )
        assert len(result.rows) == 1
        assert result.rows[0].uniform is True
        assert "alle auf allen Geraeten identisch" in result.summary

    def test_content_in_cell_for_ui_detail(self) -> None:
        """AliasCell.content (neu): UI-Detail-Aufklapp braucht den vollen Inhalt."""
        items = self._items(("ALIAS_A", "network", "10.0.0.0/24 10.0.1.0/24"))
        result = compare_aliases({"dev1": items}, columns=["dev1"])
        cell = result.rows[0].cells[0][1]
        assert cell.status == "present"
        assert cell.content == ("10.0.0.0/24", "10.0.1.0/24")

    def test_missing_on_one_device_drift(self) -> None:
        present = self._items(("ALIAS_A", "network", "10.0.0.0/24"))
        result = compare_aliases(
            {"dev1": present, "dev2": [], "dev3": present},
            columns=["dev1", "dev2", "dev3"],
        )
        row = result.rows[0]
        assert row.uniform is False
        cell_dev2 = next(c for did, c in row.cells if did == "dev2")
        assert cell_dev2.status == "absent"
        assert "Unterschieden" in result.summary

    def test_different_content_marks_drift(self) -> None:
        result = compare_aliases(
            {
                "dev1": self._items(("A", "network", "10.0.0.0/24")),
                "dev2": self._items(("A", "network", "10.0.0.0/24 10.0.1.0/24")),
            },
            columns=["dev1", "dev2"],
        )
        assert result.rows[0].uniform is False
        # Fingerprints muessen unterschiedlich sein
        fps = {c.content_fingerprint for _, c in result.rows[0].cells}
        assert len(fps) == 2

    def test_unreachable_device_does_not_break(self) -> None:
        result = compare_aliases(
            {
                "dev1": self._items(("A", "network", "10.0.0.0/24")),
                "dev2": None,  # unerreichbar
            },
            columns=["dev1", "dev2"],
        )
        row = result.rows[0]
        cell_dev2 = next(c for did, c in row.cells if did == "dev2")
        assert cell_dev2.status == "unreachable"
        # uniform-Bewertung haengt davon ab dass auch erreichbare Geraete uniform sind;
        # bei nur einem erreichbaren ist es trivially uniform=False (any_absent ist
        # False bei einem present, und fingerprints_seen=1 -> uniform=True). Pruefe
        # dass kein Crash passiert + Matrix korrekt aufgebaut.
        assert len(row.cells) == 2

    def test_no_aliases_anywhere(self) -> None:
        result = compare_aliases({"dev1": [], "dev2": []}, columns=["dev1", "dev2"])
        assert result.rows == ()
        assert "Keine Aliase" in result.summary
