"""Tests für core.validation — CIDR, Alias-Namen, Alias-Typen, Gateway-Namen."""

from __future__ import annotations

import ipaddress

import pytest

from opn_cockpit.core.errors import ValidationError
from opn_cockpit.core.validation import (
    ALIAS_NAME_MAX_LEN,
    ALLOWED_ALIAS_TYPES,
    parse_cidr,
    validate_alias_name,
    validate_alias_type,
    validate_gateway_name,
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
