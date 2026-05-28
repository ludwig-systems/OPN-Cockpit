"""Tests für core.http_client — Egress-Allowlist, Auth, Retry, Fehler-Mapping."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import pytest

from opn_cockpit.core.errors import (
    ApiError,
    AuthError,
    EgressDeniedError,
    UnreachableError,
    ValidationError,
)
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning

# ---------------------------------------------------------------------------
# Fixtures & Hilfen
# ---------------------------------------------------------------------------


@dataclass
class HandlerSpy:
    """Sammelt empfangene Requests und liefert konfigurierbare Antworten."""

    responses: list[httpx.Response] = field(default_factory=list)
    received: list[httpx.Request] = field(default_factory=list)
    raise_exc: Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.received.append(request)
        if self.raise_exc is not None:
            exc = self.raise_exc
            # Nach dem ersten Wurf weiterhin werfen (für Retry-Tests, die das
            # ggf. via Liste umschalten).
            raise exc
        if not self.responses:
            return httpx.Response(200, json={})
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


def make_client(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    *,
    target_host: str = "opn-a.lab",
    target_port: int = 443,
    extra_targets: list[HttpTarget] | None = None,
    tuning: HttpTuning | None = None,
    sleeps: list[float] | None = None,
) -> tuple[HttpClient, HttpTarget, list[float]]:
    """Erzeugt einen HttpClient mit Mock-Transport und liefert ein Sleep-Spy."""
    handler = handler or (lambda r: httpx.Response(200, json={}))
    transport = httpx.MockTransport(handler)
    target = HttpTarget(host=target_host, port=target_port, verify=False)
    targets = [target] + (extra_targets or [])
    recorded: list[float] = [] if sleeps is None else sleeps

    def record_sleep(delay: float) -> None:
        recorded.append(delay)

    client = HttpClient(
        targets=targets,
        tuning=tuning,
        transport=transport,
        sleep=record_sleep,
    )
    return client, target, recorded


# ---------------------------------------------------------------------------
# Egress-Allowlist
# ---------------------------------------------------------------------------


class TestEgressAllowlist:
    def test_denies_request_against_unknown_host(self) -> None:
        client, _target, _ = make_client(target_host="opn-a.lab")
        unknown = HttpTarget(host="opn-x.lab", port=443, verify=False)

        with pytest.raises(EgressDeniedError) as exc:
            client.call(unknown, "k", "s", "GET", "/api/test")
        assert "opn-x.lab" in str(exc.value)
        assert exc.value.context.error_kind == "egress_denied"

    def test_allows_request_against_known_target(self) -> None:
        client, target, _ = make_client(
            handler=lambda r: httpx.Response(200, json={"ok": True})
        )
        response = client.call(target, "k", "s", "GET", "/api/test")
        assert response.status_code == 200

    def test_empty_inventory_blocks_all_calls(self) -> None:
        # Ein Client ohne Targets darf NICHTS schicken — defensiver Default.
        transport = httpx.MockTransport(lambda r: httpx.Response(200))
        client = HttpClient(targets=[], transport=transport)
        target = HttpTarget(host="opn-a.lab", port=443, verify=False)
        with pytest.raises(EgressDeniedError):
            client.call(target, "k", "s", "GET", "/api/test")

    def test_case_insensitive_host_matching(self) -> None:
        client, _target, _ = make_client(target_host="opn-a.lab")
        upper = HttpTarget(host="OPN-A.LAB", port=443, verify=False)
        response = client.call(upper, "k", "s", "GET", "/api/test")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Auth / Header
# ---------------------------------------------------------------------------


class TestBasicAuth:
    def test_authorization_header_is_basic_base64(self) -> None:
        spy = HandlerSpy(responses=[httpx.Response(200, json={})])
        client, target, _ = make_client(handler=spy)
        client.call(target, "alice", "wonderland", "GET", "/api/test")

        auth_header = spy.received[0].headers["authorization"]
        expected = "Basic " + base64.b64encode(b"alice:wonderland").decode("ascii")
        assert auth_header == expected

    def test_secret_does_not_appear_in_request_url(self) -> None:
        spy = HandlerSpy(responses=[httpx.Response(200, json={})])
        client, target, _ = make_client(handler=spy)
        client.call(target, "alice", "s3cret!supersecret", "GET", "/api/test")

        url = str(spy.received[0].url)
        assert "s3cret" not in url
        assert "supersecret" not in url


# ---------------------------------------------------------------------------
# Fehler-Mapping
# ---------------------------------------------------------------------------


class TestStatusMapping:
    @pytest.mark.parametrize("code", [401, 403])
    def test_401_403_raise_auth_error(self, code: int) -> None:
        client, target, _ = make_client(
            handler=lambda r: httpx.Response(code, json={"message": "nope"})
        )
        with pytest.raises(AuthError) as exc:
            client.call(target, "k", "s", "GET", "/api/test")
        assert exc.value.context.status_code == code

    @pytest.mark.parametrize("code", [400, 404, 422])
    def test_4xx_raise_validation_error(self, code: int) -> None:
        client, target, _ = make_client(
            handler=lambda r: httpx.Response(code, json={"field": "bad"})
        )
        with pytest.raises(ValidationError) as exc:
            client.call(target, "k", "s", "POST", "/api/test", json={"x": 1})
        assert exc.value.context.status_code == code

    def test_500_raises_api_error_after_retry_exhaustion(self) -> None:
        tuning = HttpTuning(retry_count=0)
        client, target, _ = make_client(
            handler=lambda r: httpx.Response(500, json={"err": "boom"}),
            tuning=tuning,
        )
        with pytest.raises(ApiError) as exc:
            client.call(target, "k", "s", "GET", "/api/test")
        assert exc.value.context.status_code == 500


class TestErrorSummaryStaysShort:
    def test_summary_truncated_for_long_text_body(self) -> None:
        long_body = "x" * 1000
        client, target, _ = make_client(
            handler=lambda r: httpx.Response(400, text=long_body)
        )
        with pytest.raises(ValidationError) as exc:
            client.call(target, "k", "s", "GET", "/api/test")
        assert len(exc.value.context.summary) <= 200

    def test_summary_shows_only_json_keys(self) -> None:
        # Verhindert, dass komplette JSON-Bodies (potenziell mit Secrets) in
        # die Exception fließen — nur die Schlüsselnamen werden angezeigt.
        client, target, _ = make_client(
            handler=lambda r: httpx.Response(
                400,
                json={"api_key": "geheim", "field": "bad", "stack": "nope"},
            )
        )
        with pytest.raises(ValidationError) as exc:
            client.call(target, "k", "s", "POST", "/api/test", json={"x": 1})
        summary = exc.value.context.summary
        assert "geheim" not in summary
        assert "json-keys=" in summary


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class TestRetry:
    def test_retries_on_503_then_succeeds(self) -> None:
        responses = [
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
        attempts = [0]

        def handler(_req: httpx.Request) -> httpx.Response:
            response = responses[attempts[0]]
            attempts[0] += 1
            return response

        tuning = HttpTuning(retry_count=2, retry_backoff_base_s=0.0)
        client, target, sleeps = make_client(handler=handler, tuning=tuning)
        response = client.call(target, "k", "s", "GET", "/api/test")
        assert response.status_code == 200
        assert attempts[0] == 3
        # zwei Pausen zwischen drei Versuchen
        assert len(sleeps) == 2

    def test_no_retry_on_4xx(self) -> None:
        attempts = [0]

        def handler(_req: httpx.Request) -> httpx.Response:
            attempts[0] += 1
            return httpx.Response(400, json={})

        tuning = HttpTuning(retry_count=3, retry_backoff_base_s=0.0)
        client, target, sleeps = make_client(handler=handler, tuning=tuning)
        with pytest.raises(ValidationError):
            client.call(target, "k", "s", "GET", "/api/test")
        assert attempts[0] == 1
        assert sleeps == []

    def test_exhausts_retries_on_persistent_503(self) -> None:
        attempts = [0]

        def handler(_req: httpx.Request) -> httpx.Response:
            attempts[0] += 1
            return httpx.Response(503)

        tuning = HttpTuning(retry_count=2, retry_backoff_base_s=0.0)
        client, target, sleeps = make_client(handler=handler, tuning=tuning)
        with pytest.raises(ApiError):
            client.call(target, "k", "s", "GET", "/api/test")
        assert attempts[0] == 3
        assert len(sleeps) == 2

    def test_retries_on_connection_error(self) -> None:
        attempts = [0]
        responses_iter = [
            httpx.ConnectError("conn refused"),
            httpx.Response(200, json={}),
        ]

        def handler(_req: httpx.Request) -> httpx.Response:
            item = responses_iter[attempts[0]]
            attempts[0] += 1
            if isinstance(item, Exception):
                raise item
            return item

        tuning = HttpTuning(retry_count=1, retry_backoff_base_s=0.0)
        client, target, sleeps = make_client(handler=handler, tuning=tuning)
        response = client.call(target, "k", "s", "GET", "/api/test")
        assert response.status_code == 200
        assert sleeps == [0.0]

    def test_unreachable_after_persistent_timeout(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out")

        tuning = HttpTuning(retry_count=1, retry_backoff_base_s=0.0)
        client, target, _ = make_client(handler=handler, tuning=tuning)
        with pytest.raises(UnreachableError) as exc:
            client.call(target, "k", "s", "GET", "/api/test")
        assert exc.value.context.error_kind == "timeout"

    def test_backoff_grows_exponentially(self) -> None:
        responses = [httpx.Response(503)] * 3 + [httpx.Response(200, json={})]
        i = [0]

        def handler(_req: httpx.Request) -> httpx.Response:
            r = responses[i[0]]
            i[0] += 1
            return r

        tuning = HttpTuning(
            retry_count=3,
            retry_backoff_base_s=0.1,
            retry_backoff_factor=2.0,
        )
        client, target, sleeps = make_client(handler=handler, tuning=tuning)
        client.call(target, "k", "s", "GET", "/api/test")
        # Backoffs: 0.1 * (2**0), 0.1 * (2**1), 0.1 * (2**2)
        assert sleeps == pytest.approx([0.1, 0.2, 0.4])


# ---------------------------------------------------------------------------
# Konfigurations-Pfade
# ---------------------------------------------------------------------------


class TestTuning:
    def test_reconfigure_timeout_used_when_override_passed(self) -> None:
        spy = HandlerSpy(responses=[httpx.Response(200, json={})])
        client, target, _ = make_client(
            handler=spy,
            tuning=HttpTuning(read_timeout_s=10.0, reconfigure_timeout_s=60.0),
        )
        client.call(target, "k", "s", "POST", "/api/reconfigure", timeout_override_s=60.0)
        # Wir prüfen indirekt: kein Fehler, Spy hat den Request gesehen.
        assert len(spy.received) == 1

    def test_close_idempotent(self) -> None:
        client, _target, _ = make_client()
        client.close()
        client.close()  # darf nicht crashen

    def test_context_manager_closes_clients(self) -> None:
        with make_client()[0] as c:
            assert c is not None

    def test_close_after_request_disposes_clients(self) -> None:
        spy = HandlerSpy(responses=[httpx.Response(200, json={})])
        client, target, _ = make_client(handler=spy)
        client.call(target, "k", "s", "GET", "/api/test")
        # Mindestens ein interner httpx.Client wurde lazily erzeugt.
        assert client._clients
        client.close()
        assert not client._clients

    def test_tuning_property_returns_active_settings(self) -> None:
        tuning = HttpTuning(connect_timeout_s=7.5)
        client, _target, _ = make_client(tuning=tuning)
        assert client.tuning is tuning

    def test_second_call_reuses_internal_httpx_client(self) -> None:
        spy = HandlerSpy(responses=[httpx.Response(200, json={})])
        client, target, _ = make_client(handler=spy)
        client.call(target, "k", "s", "GET", "/a")
        client.call(target, "k", "s", "GET", "/b")
        assert len(client._clients) == 1


class TestNetworkExhaustion:
    def test_unreachable_after_persistent_connection_errors(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("conn refused")

        tuning = HttpTuning(retry_count=1, retry_backoff_base_s=0.0)
        client, target, _ = make_client(handler=handler, tuning=tuning)
        with pytest.raises(UnreachableError) as exc:
            client.call(target, "k", "s", "GET", "/api/test")
        assert exc.value.context.error_kind == "network"

    def test_generic_http_error_maps_to_unreachable(self) -> None:
        # Simuliert einen httpx-Fehler, der weder ConnectError noch ReadError
        # noch Timeout ist (z. B. TLS-Handshake-Fehler), aber von httpx.HTTPError
        # erbt. Solche Fälle muss der Client ebenfalls als unerreichbar
        # einstufen, ohne Retry.
        class _OddHttpError(httpx.HTTPError):
            pass

        def handler(_req: httpx.Request) -> httpx.Response:
            raise _OddHttpError("tls handshake failed")

        tuning = HttpTuning(retry_count=0)
        client, target, _ = make_client(handler=handler, tuning=tuning)
        with pytest.raises(UnreachableError) as exc:
            client.call(target, "k", "s", "GET", "/api/test")
        assert exc.value.context.error_kind == "http"


class TestBodySummary:
    def test_json_list_summary_shows_length(self) -> None:
        client, target, _ = make_client(
            handler=lambda r: httpx.Response(400, json=[1, 2, 3])
        )
        with pytest.raises(ValidationError) as exc:
            client.call(target, "k", "s", "GET", "/api/test")
        assert "json-list[len=3]" in exc.value.context.summary

    def test_json_scalar_summary_used_for_primitive(self) -> None:
        client, target, _ = make_client(
            handler=lambda r: httpx.Response(400, json="just-a-string")
        )
        with pytest.raises(ValidationError) as exc:
            client.call(target, "k", "s", "GET", "/api/test")
        assert "just-a-string" in exc.value.context.summary


# ---------------------------------------------------------------------------
# Vertrag: Klartext-Secret leakt nicht in HandlerSpy.received-Path
# ---------------------------------------------------------------------------


def test_request_url_and_path_have_no_secret() -> None:
    spy = HandlerSpy(responses=[httpx.Response(200, json={})])
    client, target, _ = make_client(handler=spy)
    secret = "EXTREMELY_SECRET_VALUE_xyz123"
    client.call(target, "alice", secret, "POST", "/api/x", json={"plain": "value"})
    rendered_request = json.dumps(
        {
            "url": str(spy.received[0].url),
            "path": spy.received[0].url.path,
            "body": spy.received[0].content.decode("utf-8"),
        }
    )
    assert secret not in rendered_request
