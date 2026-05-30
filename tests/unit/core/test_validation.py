"""Tests für core.validation — CIDR, Alias-Namen, Alias-Typen, Gateway-Namen."""

from __future__ import annotations

import ipaddress

import pytest

from opn_cockpit.core.errors import ValidationError
from opn_cockpit.core.validation import (
    ALIAS_NAME_MAX_LEN,
    ALLOWED_ALIAS_TYPES,
    parse_cidr,
    validate_alias_content,
    validate_alias_name,
    validate_alias_type,
    validate_gateway_name,
    validate_host,
    validate_port_value,
    validate_url,
)


class TestParseCidr:
    @pytest.mark.parametrize(
        "value",
        ["10.0.0.0/8", "192.168.1.0/24", "2001:db8::/32", "0.0.0.0/0"],
    )
    def test_accepts_valid(self, value: str) -> None:
        result = parse_cidr(value)
        assert isinstance(result, (ipaddress.IPv4Network, ipaddress.IPv6Network))
        assert str(result) == value

    def test_rejects_host_bits_outside_mask(self) -> None:
        with pytest.raises(ValidationError) as exc:
            parse_cidr("10.1.2.5/24")
        assert exc.value.context.error_kind == "cidr_invalid"

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError) as exc:
            parse_cidr("")
        assert exc.value.context.error_kind == "cidr_empty"

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(ValidationError):
            parse_cidr("   ")

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValidationError):
            parse_cidr("not-a-net")

    def test_strips_surrounding_whitespace(self) -> None:
        result = parse_cidr("  10.0.0.0/8  ")
        assert str(result) == "10.0.0.0/8"


class TestValidateAliasName:
    @pytest.mark.parametrize(
        "name",
        ["A", "branch_office", "Branch1", "X_2", "alias32_" + "x" * 24],
    )
    def test_accepts_valid(self, name: str) -> None:
        assert validate_alias_name(name) == name.strip()

    @pytest.mark.parametrize(
        "name",
        ["", "   ", "1starts_with_digit", "_underscore_first", "has-dash", "spaces in"],
    )
    def test_rejects_invalid(self, name: str) -> None:
        with pytest.raises(ValidationError):
            validate_alias_name(name)

    def test_rejects_overlong(self) -> None:
        too_long = "a" + "b" * ALIAS_NAME_MAX_LEN
        with pytest.raises(ValidationError) as exc:
            validate_alias_name(too_long)
        assert exc.value.context.error_kind == "alias_name_too_long"


class TestValidateAliasType:
    @pytest.mark.parametrize("value", ["host", "Network", "PORT", "url"])
    def test_accepts_known_types_case_insensitive(self, value: str) -> None:
        result = validate_alias_type(value)
        assert result == value.lower()
        assert result in ALLOWED_ALIAS_TYPES

    def test_rejects_unknown_type(self) -> None:
        with pytest.raises(ValidationError) as exc:
            validate_alias_type("frobnicate")
        assert exc.value.context.error_kind == "alias_type_unknown"

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            validate_alias_type("")


class TestValidateGatewayName:
    @pytest.mark.parametrize("name", ["V2_WANBwIn", "WAN_GW", "Gw_123"])
    def test_accepts_valid(self, name: str) -> None:
        assert validate_gateway_name(name) == name

    @pytest.mark.parametrize(
        "name",
        ["", "   ", "with space", "weird-name", "lots of words"],
    )
    def test_rejects_invalid(self, name: str) -> None:
        with pytest.raises(ValidationError):
            validate_gateway_name(name)


class TestValidateHost:
    @pytest.mark.parametrize(
        "value",
        [
            "10.0.0.1",
            "192.168.1.254",
            "2001:db8::1",
            "[2001:db8::1]",
            "opn-1.lab",
            "hq-berlin",
            "example.com",
            "subdomain.example.com",
        ],
    )
    def test_accepts_valid(self, value: str) -> None:
        assert validate_host(value) == value

    def test_strips_whitespace(self) -> None:
        assert validate_host("  10.0.0.1  ") == "10.0.0.1"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "not a host",  # space inside
            "-startsWithDash",
            "endsWithDash-",
            "with_underscore",  # _ not in hostname label
            "host..double",  # double dot
            "label." * 60,  # too long
        ],
    )
    def test_rejects_invalid(self, value: str) -> None:
        with pytest.raises(ValidationError):
            validate_host(value)


class TestValidatePortValue:
    @pytest.mark.parametrize(
        "value", ["80", "1", "65535", "80-90", "1024:2048", "80-80"],
    )
    def test_accepts_valid(self, value: str) -> None:
        assert validate_port_value(value) == value

    @pytest.mark.parametrize(
        "value",
        ["", "0", "65536", "80-90-100", "abc", "100-50", "80,90"],
    )
    def test_rejects_invalid(self, value: str) -> None:
        with pytest.raises(ValidationError):
            validate_port_value(value)


class TestValidateUrl:
    @pytest.mark.parametrize(
        "value",
        [
            "https://example.com",
            "http://opn-1.lab/list.txt",
            "https://blocklist.example.org/feed.csv",
        ],
    )
    def test_accepts_valid(self, value: str) -> None:
        assert validate_url(value) == value

    @pytest.mark.parametrize(
        "value", ["", "ftp://example.com", "example.com", "https:// space"],
    )
    def test_rejects_invalid(self, value: str) -> None:
        with pytest.raises(ValidationError):
            validate_url(value)


class TestValidateAliasContent:
    def test_host_content(self) -> None:
        assert validate_alias_content("host", ["10.0.0.1", "example.com"]) == [
            "10.0.0.1", "example.com",
        ]

    def test_network_content(self) -> None:
        assert validate_alias_content("network", ["10.0.0.0/24", "192.168.0.0/16"]) == [
            "10.0.0.0/24", "192.168.0.0/16",
        ]

    def test_port_content(self) -> None:
        assert validate_alias_content("port", ["80", "443-450"]) == ["80", "443-450"]

    def test_url_content(self) -> None:
        assert validate_alias_content("url", ["https://example.com"]) == [
            "https://example.com",
        ]

    def test_unknown_type_passes_through(self) -> None:
        # mac/asn/geoip etc. werden heute nicht strikt validiert
        assert validate_alias_content("mac", ["aa:bb:cc:dd:ee:ff"]) == [
            "aa:bb:cc:dd:ee:ff",
        ]

    def test_empty_content_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate_alias_content("host", [])

    def test_invalid_host_in_list_reported_with_position(self) -> None:
        with pytest.raises(ValidationError, match="#2"):
            validate_alias_content("host", ["10.0.0.1", "not a host", "2.2.2.2"])

    def test_invalid_network_reports_clearly(self) -> None:
        with pytest.raises(ValidationError):
            validate_alias_content("network", ["10.0.0.5/24"])

    def test_empty_entries_skipped(self) -> None:
        assert validate_alias_content("host", ["10.0.0.1", "", "2.2.2.2"]) == [
            "10.0.0.1", "2.2.2.2",
        ]
