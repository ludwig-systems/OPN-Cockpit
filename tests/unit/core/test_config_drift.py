"""Tests fuer config_drift.normalize_config_xml + compute_drift_hash."""

from __future__ import annotations

import pytest

from opn_cockpit.core.config_drift import compute_drift_hash, normalize_config_xml

OPNSENSE_BASE_XML = b"""<?xml version="1.0"?>
<opnsense>
  <version>26.1</version>
  <interfaces>
    <wan>
      <if>vmx0</if>
      <ipaddr>10.0.0.1</ipaddr>
    </wan>
  </interfaces>
  <aliases>
    <alias>
      <name>test</name>
      <content>1.2.3.4</content>
    </alias>
  </aliases>
</opnsense>
"""


class TestNormalize:
    def test_strips_revision_block(self) -> None:
        xml_with_rev = b"""<?xml version="1.0"?>
<opnsense>
  <revision>
    <time>1717000000</time>
    <description>admin@host</description>
  </revision>
  <interfaces><wan><if>vmx0</if></wan></interfaces>
</opnsense>"""
        normalized = normalize_config_xml(xml_with_rev)
        assert "<revision>" not in normalized
        assert "1717000000" not in normalized
        assert "<interfaces>" in normalized

    def test_strips_lastchange_self_closing(self) -> None:
        xml = b"<opnsense><lastchange/><foo>x</foo></opnsense>"
        normalized = normalize_config_xml(xml)
        assert "lastchange" not in normalized
        assert "<foo>x</foo>" in normalized

    def test_collapses_whitespace_between_tags(self) -> None:
        a = b"<opnsense>\n  <x>1</x>\n  <y>2</y>\n</opnsense>"
        b = b"<opnsense>\n\n  <x>1</x>\n\n\n  <y>2</y>\n</opnsense>"
        # Verschiedene Whitespace, gleiches normalisiertes Ergebnis
        assert normalize_config_xml(a) == normalize_config_xml(b)


class TestComputeDriftHash:
    def test_identical_xml_same_hash(self) -> None:
        h1 = compute_drift_hash(OPNSENSE_BASE_XML)
        h2 = compute_drift_hash(OPNSENSE_BASE_XML)
        assert h1 == h2
        # 64 Hex-Zeichen
        assert len(h1) == 64

    def test_only_revision_changed_same_hash(self) -> None:
        """Revision-Block-Aenderung darf KEIN Drift signalisieren.

        Das ist der Kern-Use-Case: OPNsense erhoeht bei jedem Save die
        Revision auch ohne sachliche Aenderung. Wer drift_detection nutzt,
        will Aenderungen am Inhalt sehen, nicht das Save-Rauschen.
        """
        rev_a = (
            b"<opnsense>\n  <revision><time>1000</time><description>admin1</description></revision>"
        )
        rev_b = (
            b"<opnsense>\n  <revision><time>2000</time><description>admin2</description></revision>"
        )
        xml_a = OPNSENSE_BASE_XML.replace(b"<opnsense>", rev_a)
        xml_b = OPNSENSE_BASE_XML.replace(b"<opnsense>", rev_b)
        assert compute_drift_hash(xml_a) == compute_drift_hash(xml_b)

    def test_real_content_change_diff_hash(self) -> None:
        modified = OPNSENSE_BASE_XML.replace(
            b"<content>1.2.3.4</content>",
            b"<content>5.6.7.8</content>",
        )
        h_original = compute_drift_hash(OPNSENSE_BASE_XML)
        h_modified = compute_drift_hash(modified)
        assert h_original != h_modified

    def test_handles_invalid_xml_without_crash(self) -> None:
        # Auch bei Schrott-Input liefern wir einen Hash zurueck
        assert len(compute_drift_hash(b"not <xml at all >")) == 64

    def test_handles_empty_bytes(self) -> None:
        assert len(compute_drift_hash(b"")) == 64


@pytest.mark.parametrize("garbage", [b"", b"<", b"\x00\x01\x02", b"<opnsense>"])
def test_no_exceptions_on_garbage(garbage: bytes) -> None:
    # Nie eine Exception, immer ein 64-stelliger Hex
    h = compute_drift_hash(garbage)
    assert isinstance(h, str)
    assert len(h) == 64
