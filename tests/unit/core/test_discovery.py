"""Tests für core.discovery — list_gateways + list_aliases."""

from __future__ import annotations

import httpx
import pytest

from opn_cockpit.core.discovery import (
    AliasSummary,
    DiscoveryError,
    GatewaySummary,
    list_aliases,
    list_gateways,
)
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning
from opn_cockpit.core.objects._endpoints import ALIAS_SEARCH, GATEWAY_STATUS


def _client_with_handler(handler) -> tuple[HttpClient, HttpTarget]:  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)
    target = HttpTarget(host="opn-lab", port=443, verify=False)
    client = HttpClient(
        targets=[target],
        tuning=HttpTuning(retry_count=0),
        transport=transport,
        sleep=lambda _delay: None,
    )
    return client, target


# ---------------------------------------------------------------------------
# Gateways
# ---------------------------------------------------------------------------


class TestListGateways:
    def test_parses_items_format(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"name": "WAN_GW", "address": "1.2.3.4", "status": "online"},
                        {"name": "LAN_GW", "address": "10.0.0.1", "status": "online"},
                    ]
                },
            )

        client, target = _client_with_handler(handler)
        result = list_gateways(client, target, "k", "s")
        assert [g.name for g in result] == ["LAN_GW", "WAN_GW"]  # alphabetisch sortiert
        assert isinstance(result[0], GatewaySummary)
        assert result[0].address == "10.0.0.1"

    def test_parses_rows_format(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"rows": [{"name": "X", "status": "down"}]},
            )

        client, target = _client_with_handler(handler)
        result = list_gateways(client, target, "k", "s")
        assert len(result) == 1
        assert result[0].name == "X"
        assert result[0].is_online is False

    def test_calls_correct_endpoint(self) -> None:
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.url.path)
            return httpx.Response(200, json={"items": []})

        client, target = _client_with_handler(handler)
        list_gateways(client, target, "k", "s")
        assert seen == [GATEWAY_STATUS]

    def test_empty_for_unknown_format(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=["wrong", "shape"])

        client, target = _client_with_handler(handler)
        assert list_gateways(client, target, "k", "s") == []

    def test_empty_for_non_json(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not-json")

        client, target = _client_with_handler(handler)
        assert list_gateways(client, target, "k", "s") == []

    def test_skips_items_without_name(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"items": [{"address": "x"}, {"name": "ok"}]},
            )

        client, target = _client_with_handler(handler)
        result = list_gateways(client, target, "k", "s")
        assert [g.name for g in result] == ["ok"]

    def test_wraps_auth_error_as_discovery_error(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "nope"})

        client, target = _client_with_handler(handler)
        with pytest.raises(DiscoveryError) as exc:
            list_gateways(client, target, "k", "s")
        assert "opn-lab" in str(exc.value)


class TestIsOnline:
    @pytest.mark.parametrize("status", ["online", "up", "ok", "OK", "ONLINE", ""])
    def test_treats_known_states_as_online(self, status: str) -> None:
        g = GatewaySummary(name="X", status=status)
        assert g.is_online is True

    @pytest.mark.parametrize("status", ["down", "fail", "loss"])
    def test_treats_unknown_states_as_offline(self, status: str) -> None:
        g = GatewaySummary(name="X", status=status)
        assert g.is_online is False


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


class TestListAliases:
    def test_parses_rows_format(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "rows": [
                        {"name": "branch_ips", "type": "host", "description": "HQ"},
                        {"name": "lab_ports", "type": "port"},
                    ]
                },
            )

        client, target = _client_with_handler(handler)
        result = list_aliases(client, target, "k", "s")
        assert [a.name for a in result] == ["branch_ips", "lab_ports"]
        assert isinstance(result[0], AliasSummary)
        assert result[0].descr == "HQ"

    def test_supports_descr_fallback(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"rows": [{"name": "x", "type": "host", "descr": "Lab"}]},
            )

        client, target = _client_with_handler(handler)
        result = list_aliases(client, target, "k", "s")
        assert result[0].descr == "Lab"

    def test_calls_correct_endpoint_with_empty_filter(self) -> None:
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.url.path)
            return httpx.Response(200, json={"rows": []})

        client, target = _client_with_handler(handler)
        list_aliases(client, target, "k", "s")
        assert seen == [ALIAS_SEARCH]

    def test_empty_for_non_dict_root(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client, target = _client_with_handler(handler)
        assert list_aliases(client, target, "k", "s") == []

    def test_skips_rows_without_name(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"rows": [{"type": "host"}, {"name": "ok"}]},
            )

        client, target = _client_with_handler(handler)
        result = list_aliases(client, target, "k", "s")
        assert [a.name for a in result] == ["ok"]

    def test_wraps_unreachable_as_discovery_error(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline")

        client, target = _client_with_handler(handler)
        with pytest.raises(DiscoveryError):
            list_aliases(client, target, "k", "s")
