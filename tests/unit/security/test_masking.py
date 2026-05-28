"""Tests für security.masking — MaskedStr und mask_dict.

Kritisch: MaskedStr darf in keinem Conversion-Pfad den Klartext preisgeben,
weder via ``str()``, ``repr()``, f-string, ``%s`` noch via Konkatenation.
"""

from __future__ import annotations

import pytest

from opn_cockpit.security.masking import (
    MASK_TOKEN,
    MaskedStr,
    mask_dict,
    mask_secret,
)


class TestMaskedStrConversionPaths:
    def test_str_returns_mask(self) -> None:
        m = MaskedStr("topsecret")
        assert str(m) == MASK_TOKEN

    def test_repr_returns_mask(self) -> None:
        m = MaskedStr("topsecret")
        assert repr(m) == MASK_TOKEN

    def test_fstring_returns_mask(self) -> None:
        m = MaskedStr("topsecret")
        assert f"{m}" == MASK_TOKEN
        # mit Format-Spec
        assert f"{m:>10}" == MASK_TOKEN

    def test_percent_s_returns_mask(self) -> None:
        m = MaskedStr("topsecret")
        # Bewusster Test des veralteten %-Operators — wir wollen sicher sein,
        # dass auch dieser Conversion-Pfad maskiert.
        assert "%s" % m == MASK_TOKEN  # noqa: UP031

    def test_concatenation_with_str_raises(self) -> None:
        m = MaskedStr("topsecret")
        with pytest.raises(TypeError):
            "prefix" + m  # type: ignore[operator]
        with pytest.raises(TypeError):
            m + "suffix"  # type: ignore[operator]

    def test_reveal_returns_clear_text(self) -> None:
        m = MaskedStr("topsecret")
        assert m.reveal() == "topsecret"


class TestMaskedStrSemantics:
    def test_equality_by_underlying_value(self) -> None:
        assert MaskedStr("x") == MaskedStr("x")
        assert MaskedStr("x") != MaskedStr("y")

    def test_hash_consistent_with_equality(self) -> None:
        assert hash(MaskedStr("x")) == hash(MaskedStr("x"))

    def test_truthiness_follows_underlying(self) -> None:
        assert MaskedStr("not-empty")
        assert not MaskedStr("")

    def test_does_not_compare_equal_to_plain_str(self) -> None:
        # Wichtig: ein nackter Vergleich mit der Klartext-Form soll
        # NICHT match'en — sonst könnte ein test `assert x == "secret"`
        # versehentlich grün laufen und die Maskierung verstecken.
        assert MaskedStr("secret") != "secret"


class TestMaskSecret:
    def test_wraps_string(self) -> None:
        m = mask_secret("hello")
        assert isinstance(m, MaskedStr)
        assert m.reveal() == "hello"


class TestMaskDict:
    def test_masks_known_secret_keys(self) -> None:
        out = mask_dict(
            {
                "api_key": "k",
                "api_secret": "s",
                "password": "p",
                "token": "t",
                "authorization": "a",
                "name": "fine",
                "host": "h",
            }
        )
        for k in ("api_key", "api_secret", "password", "token", "authorization"):
            assert out[k] == MASK_TOKEN
        assert out["name"] == "fine"
        assert out["host"] == "h"

    def test_case_insensitive_key_match(self) -> None:
        out = mask_dict({"API_KEY": "k", "Password": "p"})
        assert out["API_KEY"] == MASK_TOKEN
        assert out["Password"] == MASK_TOKEN

    def test_nested_dict_recursion(self) -> None:
        out = mask_dict(
            {"device": {"name": "X", "api_secret": "S"}}
        )
        assert out["device"]["api_secret"] == MASK_TOKEN
        assert out["device"]["name"] == "X"

    def test_list_of_dicts_recursion(self) -> None:
        out = mask_dict(
            {
                "devices": [
                    {"name": "A", "api_key": "K1"},
                    {"name": "B", "api_key": "K2"},
                ]
            }
        )
        assert out["devices"][0]["api_key"] == MASK_TOKEN
        assert out["devices"][1]["name"] == "B"

    def test_non_secret_values_untouched(self) -> None:
        payload = {"count": 42, "tags": ["a", "b"], "enabled": True}
        out = mask_dict(payload)
        assert out["count"] == 42
        assert out["tags"] == ["a", "b"]
        assert out["enabled"] is True

    def test_preserves_original_dict(self) -> None:
        original = {"api_key": "K"}
        mask_dict(original)
        assert original["api_key"] == "K"  # nicht verändert
