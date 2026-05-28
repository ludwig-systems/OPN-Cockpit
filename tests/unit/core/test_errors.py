"""Tests für core.errors — Strukturierte Fehler ohne Rohdaten-Leaks."""

from __future__ import annotations

from opn_cockpit.core.errors import (
    ApiError,
    AuthError,
    EgressDeniedError,
    ErrorContext,
    OpnCockpitError,
    ReconfigureError,
    UnreachableError,
    ValidationError,
    VerificationError,
    make_context,
)


class TestErrorHierarchy:
    def test_all_inherit_from_base(self) -> None:
        for cls in (
            EgressDeniedError,
            UnreachableError,
            AuthError,
            ValidationError,
            ApiError,
            ReconfigureError,
            VerificationError,
        ):
            assert issubclass(cls, OpnCockpitError)

    def test_default_kind_set_per_class(self) -> None:
        err = AuthError("nope")
        assert err.context.error_kind == "auth"

    def test_custom_context_preserved(self) -> None:
        ctx = make_context(host="opnsense.lab", port=443, status_code=401)
        err = AuthError("nope", context=ctx)
        assert err.context.host == "opnsense.lab"
        assert err.context.status_code == 401


class TestMakeContext:
    def test_truncates_long_summaries(self) -> None:
        long_body = "x" * 500
        ctx = make_context(summary=long_body, summary_max_len=100)
        assert len(ctx.summary) <= 100
        assert ctx.summary.endswith("…")

    def test_preserves_short_summaries(self) -> None:
        ctx = make_context(summary="kurz", summary_max_len=200)
        assert ctx.summary == "kurz"

    def test_as_dict_serialization(self) -> None:
        ctx = make_context(host="h", port=443, status_code=500, summary="boom")
        data = ctx.as_dict()
        assert data["host"] == "h"
        assert data["status_code"] == 500
        assert data["summary"] == "boom"


class TestErrorContextHasNoRawBodyField:
    """Vertragstest: ``ErrorContext`` führt explizit KEIN ``body``-/``raw``-Feld.

    Wenn jemand das später einbaut, fällt es hier auf. Schutz gegen
    versehentliche Logleaks von HTTP-Antworten.
    """

    def test_no_body_field(self) -> None:
        slots = ErrorContext.__slots__
        forbidden = {"body", "raw", "response", "payload"}
        assert not (set(slots) & forbidden), (
            "ErrorContext darf keine rohen Antwortfelder enthalten — bitte "
            "stattdessen `summary` benutzen und vorab maskieren."
        )


def test_chaining_preserves_cause() -> None:
    original = RuntimeError("low-level")
    try:
        try:
            raise original
        except RuntimeError as e:
            raise UnreachableError("wrapped") from e
    except UnreachableError as wrapped:
        assert wrapped.__cause__ is original
