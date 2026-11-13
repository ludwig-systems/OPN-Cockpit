"""Tests fuer importers/unbound_csv.py (Parser + Writer)."""

from __future__ import annotations

from opn_cockpit.core.objects.unbound import UnboundForwardSpec, UnboundHostSpec
from opn_cockpit.importers.unbound_csv import (
    parse_unbound_forwards_csv,
    parse_unbound_hosts_csv,
    write_unbound_forwards_csv,
    write_unbound_hosts_csv,
)

# ===========================================================================
# Host-Overrides
# ===========================================================================


class TestHostsRoundTrip:
    def test_write_then_parse(self) -> None:
        specs = [
            UnboundHostSpec(
                host="router",
                domain="lab.local",
                server="10.0.0.1",
                description="Main",
                enabled=True,
            ),
            UnboundHostSpec(
                host="switch", domain="lab.local", server="10.0.0.2", description="", enabled=False
            ),
        ]
        csv_text = write_unbound_hosts_csv(specs)
        result = parse_unbound_hosts_csv(csv_text)
        assert not result.has_errors
        assert len(result.specs) == 2
        assert result.specs[0].host == "router"
        assert result.specs[0].enabled is True
        assert result.specs[1].enabled is False

    def test_csv_starts_with_bom(self) -> None:
        csv_text = write_unbound_hosts_csv([])
        # Excel-Kompat: erste Bytes sind UTF-8 BOM
        assert csv_text.startswith("﻿")


class TestHostsParsing:
    def test_missing_required_column(self) -> None:
        csv_text = "host,domain\nrouter,lab.local\n"
        result = parse_unbound_hosts_csv(csv_text)
        assert result.has_errors
        assert "server" in result.errors[0].lower()

    def test_empty_line_skipped(self) -> None:
        csv_text = "host,domain,server\nrouter,lab.local,10.0.0.1\n\nswitch,lab.local,10.0.0.2\n"
        result = parse_unbound_hosts_csv(csv_text)
        assert not result.has_errors
        assert len(result.specs) == 2

    def test_comment_line_skipped(self) -> None:
        csv_text = (
            "# HQ Berlin lab-DNS Snapshot 2026-07-20\n"
            "host,domain,server\n"
            "router,lab.local,10.0.0.1\n"
        )
        result = parse_unbound_hosts_csv(csv_text)
        assert not result.has_errors
        assert len(result.specs) == 1

    def test_case_insensitive_header(self) -> None:
        csv_text = "Host,DOMAIN,Server,Description,Enabled\nrouter,lab.local,10.0.0.1,Main,true\n"
        result = parse_unbound_hosts_csv(csv_text)
        assert not result.has_errors
        assert result.specs[0].description == "Main"

    def test_enabled_variants(self) -> None:
        csv_text = (
            "host,domain,server,enabled\n"
            "a,d,10.0.0.1,true\n"
            "b,d,10.0.0.2,false\n"
            "c,d,10.0.0.3,1\n"
            "e,d,10.0.0.4,0\n"
            "f,d,10.0.0.5,ja\n"
            "g,d,10.0.0.6,nein\n"
            "h,d,10.0.0.7,\n"
        )
        result = parse_unbound_hosts_csv(csv_text)
        assert not result.has_errors
        assert [s.enabled for s in result.specs] == [
            True,
            False,
            True,
            False,
            True,
            False,
            True,
        ]

    def test_missing_host_reports_row_num(self) -> None:
        csv_text = "host,domain,server\nrouter,lab.local,10.0.0.1\n,lab.local,10.0.0.2\n"
        result = parse_unbound_hosts_csv(csv_text)
        assert result.has_errors
        assert len(result.specs) == 1  # zweite Zeile abgelehnt
        assert "Zeile 3" in result.errors[0]

    def test_bom_prefix_stripped(self) -> None:
        # Excel schreibt BOM. Parser muss das schlucken.
        csv_text = "﻿host,domain,server\nrouter,lab.local,10.0.0.1\n"
        result = parse_unbound_hosts_csv(csv_text)
        assert not result.has_errors
        assert len(result.specs) == 1
        assert result.specs[0].host == "router"


# ===========================================================================
# Query-Forwards
# ===========================================================================


class TestForwardsRoundTrip:
    def test_write_then_parse_with_dot(self) -> None:
        specs = [
            UnboundForwardSpec(
                domain="",
                server="1.1.1.1",
                port=853,
                type="dot",
                verify="cloudflare-dns.com",
                description="Cloudflare DoT",
                enabled=True,
            ),
            UnboundForwardSpec(
                domain="corp.example.com",
                server="10.0.0.53",
                port=53,
                type="forward",
                verify="",
                description="AD DNS",
                enabled=True,
            ),
        ]
        csv_text = write_unbound_forwards_csv(specs)
        result = parse_unbound_forwards_csv(csv_text)
        assert not result.has_errors
        assert len(result.specs) == 2
        assert result.specs[0].type == "dot"
        assert result.specs[0].port == 853
        assert result.specs[1].domain == "corp.example.com"


class TestForwardsParsing:
    def test_missing_server_required(self) -> None:
        csv_text = "domain,server,port\na,,53\n"
        result = parse_unbound_forwards_csv(csv_text)
        assert result.has_errors  # Zeile-Fehler, nicht Header-Fehler
        assert "Zeile 2" in result.errors[0]

    def test_invalid_port_rejected(self) -> None:
        csv_text = "server,port\n1.1.1.1,99999\n"
        result = parse_unbound_forwards_csv(csv_text)
        assert result.has_errors
        assert "99999" in result.errors[0]

    def test_non_numeric_port_rejected(self) -> None:
        csv_text = "server,port\n1.1.1.1,abc\n"
        result = parse_unbound_forwards_csv(csv_text)
        assert result.has_errors
        assert "abc" in result.errors[0]

    def test_default_port_when_empty(self) -> None:
        csv_text = "server\n1.1.1.1\n"
        result = parse_unbound_forwards_csv(csv_text)
        assert not result.has_errors
        assert result.specs[0].port == 53

    def test_type_only_forward_or_dot(self) -> None:
        csv_text = "server,type\n1.1.1.1,https\n"
        result = parse_unbound_forwards_csv(csv_text)
        assert result.has_errors
        assert "https" in result.errors[0]

    def test_type_default_forward(self) -> None:
        csv_text = "server\n1.1.1.1\n"
        result = parse_unbound_forwards_csv(csv_text)
        assert not result.has_errors
        assert result.specs[0].type == "forward"
