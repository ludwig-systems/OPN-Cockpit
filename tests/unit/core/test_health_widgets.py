"""Tests fuer core/health_widgets.py — CARP, Interfaces, NTP.

Fokus:
* Response-Parsing (mehrere OPNsense-Schema-Varianten)
* State-Klassifikation (ok/warn/fail/unknown)
* Defensive Fehlerpfade: 404, Auth, Unreachable
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from opn_cockpit.core.health_widgets import (
    CARP_STATUS_ENDPOINT,
    INTERFACES_INFO_ENDPOINT,
    INTERFACES_TRAFFIC_ENDPOINT,
    NTP_STATUS_ENDPOINT_CANDIDATES,
    STATE_OK,
    STATE_UNKNOWN,
    STATE_WARN,
    fetch_carp_status,
    fetch_interfaces_detailed,
    fetch_interfaces_status,
    fetch_ntp_status,
)
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning


@dataclass
class _Route:
    path: str
    handler: Callable[[httpx.Request], httpx.Response]


@dataclass
class MockApi:
    routes: list[_Route] = field(default_factory=list)
    received: list[httpx.Request] = field(default_factory=list)

    def on(self, path: str, responder: Callable[[httpx.Request], httpx.Response]) -> MockApi:
        self.routes.append(_Route(path=path, handler=responder))
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.received.append(request)
        for route in self.routes:
            if request.url.path == route.path:
                return route.handler(request)
        return httpx.Response(404, json={"error": "no route"})


def _client(api: MockApi) -> tuple[HttpClient, HttpTarget]:
    transport = httpx.MockTransport(api)
    target = HttpTarget(host="opn-a.lab", port=443, verify=False)
    client = HttpClient(
        targets=[target],
        tuning=HttpTuning(retry_count=0, retry_backoff_base_s=0.0),
        transport=transport,
        sleep=lambda _delay: None,
    )
    return client, target


# ===========================================================================
# CARP
# ===========================================================================


class TestCarpStatus:
    def test_all_master(self) -> None:
        api = MockApi().on(
            CARP_STATUS_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {"interface": "wan", "vhid": "1", "status": "MASTER"},
                        {"interface": "lan", "vhid": "2", "status": "MASTER"},
                    ]
                },
            ),
        )
        client, target = _client(api)
        s = fetch_carp_status(client, target, "k", "s")
        assert s.state == STATE_OK
        assert s.vip_count == 2
        assert s.master_count == 2
        assert "MASTER" in s.summary

    def test_split_master_backup(self) -> None:
        api = MockApi().on(
            CARP_STATUS_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {"status": "MASTER"},
                        {"status": "BACKUP"},
                    ]
                },
            ),
        )
        client, target = _client(api)
        s = fetch_carp_status(client, target, "k", "s")
        assert s.state == STATE_WARN
        assert s.master_count == 1
        assert s.backup_count == 1

    def test_no_vips_configured(self) -> None:
        api = MockApi().on(
            CARP_STATUS_ENDPOINT,
            lambda _req: httpx.Response(200, json={"rows": []}),
        )
        client, target = _client(api)
        s = fetch_carp_status(client, target, "k", "s")
        assert s.state == STATE_UNKNOWN
        assert s.vip_count == 0
        assert "Kein HA-Setup" in s.summary

    def test_maintenance_mode_warns(self) -> None:
        api = MockApi().on(
            CARP_STATUS_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [{"status": "BACKUP"}],
                    "maintenancemode": "1",
                },
            ),
        )
        client, target = _client(api)
        s = fetch_carp_status(client, target, "k", "s")
        assert s.state == STATE_WARN
        assert s.maintenance_mode is True
        assert "Maintenance" in s.summary

    def test_init_state_warns(self) -> None:
        api = MockApi().on(
            CARP_STATUS_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {"status": "MASTER"},
                        {"status": "INIT"},
                    ]
                },
            ),
        )
        client, target = _client(api)
        s = fetch_carp_status(client, target, "k", "s")
        assert s.state == STATE_WARN
        assert s.init_count == 1

    def test_endpoint_404_unknown_state(self) -> None:
        api = MockApi().on(
            CARP_STATUS_ENDPOINT,
            lambda _req: httpx.Response(404, json={"error": "not found"}),
        )
        client, target = _client(api)
        s = fetch_carp_status(client, target, "k", "s")
        assert s.state == STATE_UNKNOWN
        assert s.endpoint_available is False
        assert s.reachable is True

    def test_auth_error(self) -> None:
        api = MockApi().on(
            CARP_STATUS_ENDPOINT,
            lambda _req: httpx.Response(401, json={"error": "auth"}),
        )
        client, target = _client(api)
        s = fetch_carp_status(client, target, "k", "s")
        assert s.authenticated is False
        assert s.state == STATE_UNKNOWN


# ===========================================================================
# Interfaces
# ===========================================================================


class TestInterfacesStatus:
    def test_all_up(self) -> None:
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {"identifier": "wan", "status": "up", "enabled": True},
                        {"identifier": "lan", "status": "up", "enabled": True},
                    ]
                },
            ),
        )
        client, target = _client(api)
        s = fetch_interfaces_status(client, target, "k", "s")
        assert s.state == STATE_OK
        assert s.up_count == 2
        assert s.down_count == 0
        assert s.total == 2

    def test_one_down_below_25_percent_stays_ok(self) -> None:
        # 4 interfaces, 1 down = 25% -> ok (Grenze ist "> 25%")
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {"identifier": "wan", "status": "up", "enabled": True},
                        {"identifier": "lan", "status": "up", "enabled": True},
                        {"identifier": "opt1", "status": "up", "enabled": True},
                        {"identifier": "opt2", "status": "down", "enabled": True},
                    ]
                },
            ),
        )
        client, target = _client(api)
        s = fetch_interfaces_status(client, target, "k", "s")
        assert s.state == STATE_OK
        assert s.down_count == 1
        assert "opt2" in s.down_names

    def test_many_down_warns(self) -> None:
        # 3 total, 2 down > 25% -> warn
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {"identifier": "wan", "status": "up", "enabled": True},
                        {"identifier": "lan", "status": "down", "enabled": True},
                        {"identifier": "opt1", "status": "down", "enabled": True},
                    ]
                },
            ),
        )
        client, target = _client(api)
        s = fetch_interfaces_status(client, target, "k", "s")
        assert s.state == STATE_WARN
        assert s.down_count == 2

    def test_disabled_interfaces_ignored(self) -> None:
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {"identifier": "wan", "status": "up", "enabled": True},
                        {"identifier": "unused", "status": "down", "enabled": False},
                    ]
                },
            ),
        )
        client, target = _client(api)
        s = fetch_interfaces_status(client, target, "k", "s")
        assert s.total == 1
        assert s.up_count == 1
        assert s.down_count == 0

    def test_dict_of_rows_supported(self) -> None:
        # OPNsense-Variante: {"rows": {"wan": {...}, "lan": {...}}}
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": {
                        "wan": {"identifier": "wan", "status": "up"},
                        "lan": {"identifier": "lan", "status": "up"},
                    }
                },
            ),
        )
        client, target = _client(api)
        s = fetch_interfaces_status(client, target, "k", "s")
        assert s.total == 2
        assert s.up_count == 2

    def test_404_unknown(self) -> None:
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(404, json={}),
        )
        client, target = _client(api)
        s = fetch_interfaces_status(client, target, "k", "s")
        assert s.state == STATE_UNKNOWN
        assert s.endpoint_available is False

    def test_unassigned_interfaces_dropped_via_empty_identifier(self) -> None:
        # User-Report 2026-07-27: 5/7 up gezeigt, aber 2 davon sind
        # nicht zugewiesene Ports. Erwartung: 5/5.
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {"identifier": "wan", "device": "em0", "status": "up", "enabled": True},
                        {"identifier": "lan", "device": "em1", "status": "up", "enabled": True},
                        {"identifier": "opt1", "device": "em2", "status": "up", "enabled": True},
                        {"identifier": "opt2", "device": "em3", "status": "up", "enabled": True},
                        {"identifier": "opt3", "device": "em4", "status": "up", "enabled": True},
                        # 2 Unassigned - leerer identifier
                        {"identifier": "", "device": "em5", "status": "down", "enabled": True},
                        {"identifier": "", "device": "em6", "status": "down", "enabled": True},
                    ]
                },
            ),
        )
        client, target = _client(api)
        s = fetch_interfaces_status(client, target, "k", "s")
        assert s.total == 5
        assert s.up_count == 5
        assert s.down_count == 0
        assert s.state == STATE_OK
        assert s.summary == "5/5 up"

    def test_unassigned_dropped_via_explicit_flag(self) -> None:
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {"identifier": "wan", "device": "em0", "status": "up", "enabled": True},
                        # Identifier ist zwar da, aber unassigned=true explizit
                        {
                            "identifier": "opt99",
                            "device": "em5",
                            "status": "down",
                            "enabled": True,
                            "unassigned": True,
                        },
                    ]
                },
            ),
        )
        client, target = _client(api)
        s = fetch_interfaces_status(client, target, "k", "s")
        assert s.total == 1
        assert s.up_count == 1
        assert s.down_count == 0

    def test_unassigned_dropped_via_identifier_equals_device(self) -> None:
        # Alternative OPNsense-Variante: identifier ist der OS-Device-Name
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {"identifier": "wan", "device": "em0", "status": "up", "enabled": True},
                        {"identifier": "em5", "device": "em5", "status": "down", "enabled": True},
                    ]
                },
            ),
        )
        client, target = _client(api)
        s = fetch_interfaces_status(client, target, "k", "s")
        assert s.total == 1
        assert s.up_count == 1

    def test_detail_still_shows_unassigned(self) -> None:
        # Regressions-Sicherung: der Detail-Tab (fetch_interfaces_detailed)
        # muss die Unassigned WEITERHIN liefern. Der Widget-Filter darf
        # nicht die Detail-Sicht kontaminieren.
        from opn_cockpit.core.health_widgets import (
            fetch_interfaces_detailed,
        )

        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {"identifier": "wan", "device": "em0", "status": "up", "enabled": True},
                        {"identifier": "", "device": "em5", "status": "down", "enabled": True},
                    ]
                },
            ),
        )
        client, target = _client(api)
        r = fetch_interfaces_detailed(client, target, "k", "s")
        # Beide Zeilen sind da (Detail-Filter ignoriert nur wenn identifier
        # UND device leer sind — was hier nicht der Fall ist).
        idents_and_devices = {(i.identifier, i.device) for i in r.interfaces}
        assert ("wan", "em0") in idents_and_devices
        assert ("", "em5") in idents_and_devices


# ===========================================================================
# NTP
# ===========================================================================


class TestNtpStatus:
    def test_system_time_reported(self) -> None:
        # Erster Kandidat liefert date-Feld -> ok
        first = NTP_STATUS_ENDPOINT_CANDIDATES[0]
        api = MockApi().on(
            first,
            lambda _req: httpx.Response(
                200,
                json={
                    "date": "2026-07-20 21:30:00",
                },
            ),
        )
        client, target = _client(api)
        s = fetch_ntp_status(client, target, "k", "s")
        assert s.state == STATE_OK
        assert s.system_time == "2026-07-20 21:30:00"

    def test_fallback_to_second_candidate(self) -> None:
        first = NTP_STATUS_ENDPOINT_CANDIDATES[0]
        second = NTP_STATUS_ENDPOINT_CANDIDATES[1]
        api = (
            MockApi()
            .on(first, lambda _req: httpx.Response(404, json={}))
            .on(
                second,
                lambda _req: httpx.Response(
                    200,
                    json={
                        "date": "2026-07-20 12:00:00",
                    },
                ),
            )
        )
        client, target = _client(api)
        s = fetch_ntp_status(client, target, "k", "s")
        assert s.state == STATE_OK
        assert s.system_time == "2026-07-20 12:00:00"

    def test_all_candidates_missing(self) -> None:
        api = MockApi()  # nichts registriert -> alle antworten 404
        client, target = _client(api)
        s = fetch_ntp_status(client, target, "k", "s")
        assert s.state == STATE_UNKNOWN
        assert s.endpoint_available is False

    def test_endpoint_present_but_no_time_field(self) -> None:
        first = NTP_STATUS_ENDPOINT_CANDIDATES[0]
        api = MockApi().on(
            first,
            lambda _req: httpx.Response(200, json={"foo": "bar"}),
        )
        client, target = _client(api)
        s = fetch_ntp_status(client, target, "k", "s")
        assert s.state == STATE_UNKNOWN
        assert s.endpoint_available is True
        assert s.system_time == ""

    def test_nested_system_time(self) -> None:
        first = NTP_STATUS_ENDPOINT_CANDIDATES[0]
        api = MockApi().on(
            first,
            lambda _req: httpx.Response(
                200,
                json={
                    "system": {"date": "Fri Jul 20 2026"},
                },
            ),
        )
        client, target = _client(api)
        s = fetch_ntp_status(client, target, "k", "s")
        assert s.state == STATE_OK
        assert s.system_time == "Fri Jul 20 2026"


# ===========================================================================
# Interfaces-Detail (Device-Modal-Tab)
# ===========================================================================


class TestInterfacesDetailed:
    def test_full_detail_parsed(self) -> None:
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {
                            "identifier": "wan",
                            "description": "HQ WAN Uplink",
                            "device": "vtnet0",
                            "enabled": True,
                            "status": "up",
                            "link": "up",
                            "ipv4": [{"ipaddr": "203.0.113.4", "subnetbits": 24}],
                            "ipv6": [{"ipaddr": "fe80::1", "subnetbits": 64}],
                            "mtu": "1500",
                            "macaddr": "00:11:22:33:44:55",
                            "media": "1000baseTX <full-duplex>",
                        },
                    ]
                },
            ),
        )
        client, target = _client(api)
        r = fetch_interfaces_detailed(client, target, "k", "s")
        assert r.reachable and r.authenticated and r.endpoint_available
        assert len(r.interfaces) == 1
        wan = r.interfaces[0]
        assert wan.identifier == "wan"
        assert wan.description == "HQ WAN Uplink"
        assert wan.device == "vtnet0"
        assert wan.enabled is True
        assert wan.link_up is True
        assert wan.ipv4 == "203.0.113.4"
        assert wan.ipv4_subnetbits == 24
        assert wan.ipv6 == "fe80::1"
        assert wan.mtu == "1500"
        assert wan.macaddr == "00:11:22:33:44:55"

    def test_disabled_interface_still_listed(self) -> None:
        # Deaktivierte Interfaces sollen im Tab sichtbar sein (nur mit
        # grauem Statuspunkt) - anders als im Kachel-Widget wo sie ignoriert
        # werden.
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {"identifier": "opt3", "enabled": False, "device": "em3"},
                    ]
                },
            ),
        )
        client, target = _client(api)
        r = fetch_interfaces_detailed(client, target, "k", "s")
        assert len(r.interfaces) == 1
        assert r.interfaces[0].enabled is False

    def test_ipv4_string_variant(self) -> None:
        # Manche OPNsense-Versionen liefern die IP als "1.2.3.4/24"-String
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {
                            "identifier": "wan",
                            "device": "em0",
                            "enabled": True,
                            "ipv4": "10.0.0.1/24",
                        },
                    ]
                },
            ),
        )
        client, target = _client(api)
        r = fetch_interfaces_detailed(client, target, "k", "s")
        wan = r.interfaces[0]
        assert wan.ipv4 == "10.0.0.1"
        assert wan.ipv4_subnetbits == 24

    def test_no_ip_configured(self) -> None:
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {"identifier": "opt1", "device": "em1", "enabled": True, "status": "up"},
                    ]
                },
            ),
        )
        client, target = _client(api)
        r = fetch_interfaces_detailed(client, target, "k", "s")
        assert r.interfaces[0].ipv4 == ""
        assert r.interfaces[0].ipv4_subnetbits == 0

    def test_sorted_by_identifier(self) -> None:
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {"identifier": "opt2", "device": "em2", "enabled": True},
                        {"identifier": "lan", "device": "em1", "enabled": True},
                        {"identifier": "wan", "device": "em0", "enabled": True},
                    ]
                },
            ),
        )
        client, target = _client(api)
        r = fetch_interfaces_detailed(client, target, "k", "s")
        idents = [i.identifier for i in r.interfaces]
        assert idents == ["lan", "opt2", "wan"]

    def test_dict_rows_supported(self) -> None:
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": {
                        "wan": {"identifier": "wan", "device": "em0", "enabled": True},
                        "lan": {"identifier": "lan", "device": "em1", "enabled": True},
                    }
                },
            ),
        )
        client, target = _client(api)
        r = fetch_interfaces_detailed(client, target, "k", "s")
        assert len(r.interfaces) == 2

    def test_404_endpoint_unavailable(self) -> None:
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(404, json={}),
        )
        client, target = _client(api)
        r = fetch_interfaces_detailed(client, target, "k", "s")
        assert r.endpoint_available is False
        assert r.interfaces == ()

    def test_traffic_counters_merged_by_os_device_name(self) -> None:
        # OPNsense mappt Traffic-Stats auf OS-Device (em0), Interfaces-
        # Info hat sowohl identifier (wan) als auch device (em0). Der
        # Merge muss ueber device gehen.
        api = (
            MockApi()
            .on(
                INTERFACES_INFO_ENDPOINT,
                lambda _req: httpx.Response(
                    200,
                    json={
                        "rows": [
                            {"identifier": "wan", "device": "em0", "enabled": True},
                            {"identifier": "lan", "device": "em1", "enabled": True},
                        ]
                    },
                ),
            )
            .on(
                INTERFACES_TRAFFIC_ENDPOINT,
                lambda _req: httpx.Response(
                    200,
                    json={
                        "interfaces": {
                            "em0": {
                                "bytes received": "1234567890",
                                "bytes transmitted": "9876543210",
                                "packets received": "10000",
                                "packets transmitted": "8000",
                            },
                            "em1": {
                                "bytes received": "42",
                                "bytes transmitted": "0",
                                "packets received": "1",
                                "packets transmitted": "0",
                            },
                        }
                    },
                ),
            )
        )
        client, target = _client(api)
        r = fetch_interfaces_detailed(client, target, "k", "s")
        by_id = {i.identifier: i for i in r.interfaces}
        assert by_id["wan"].bytes_received == 1234567890
        assert by_id["wan"].bytes_transmitted == 9876543210
        assert by_id["wan"].packets_received == 10000
        assert by_id["wan"].packets_transmitted == 8000
        assert by_id["lan"].bytes_received == 42
        assert by_id["lan"].packets_transmitted == 0

    def test_traffic_endpoint_missing_stays_zero(self) -> None:
        # 404 auf Traffic-Endpoint darf den Interface-Tab nicht killen,
        # nur die Counter bleiben 0.
        api = MockApi().on(
            INTERFACES_INFO_ENDPOINT,
            lambda _req: httpx.Response(
                200,
                json={
                    "rows": [
                        {"identifier": "wan", "device": "em0", "enabled": True},
                    ]
                },
            ),
        )
        # Traffic-Endpoint bewusst NICHT registriert -> MockApi 404
        client, target = _client(api)
        r = fetch_interfaces_detailed(client, target, "k", "s")
        assert len(r.interfaces) == 1
        wan = r.interfaces[0]
        assert wan.bytes_received == 0
        assert wan.bytes_transmitted == 0
        assert wan.packets_received == 0

    def test_traffic_underscore_keys_supported(self) -> None:
        # Aeltere OPNsense-Versionen liefern die Keys mit Unterstrich
        # statt Leerzeichen.
        api = (
            MockApi()
            .on(
                INTERFACES_INFO_ENDPOINT,
                lambda _req: httpx.Response(
                    200,
                    json={
                        "rows": [
                            {"identifier": "wan", "device": "vtnet0", "enabled": True},
                        ]
                    },
                ),
            )
            .on(
                INTERFACES_TRAFFIC_ENDPOINT,
                lambda _req: httpx.Response(
                    200,
                    json={
                        "interfaces": {
                            "vtnet0": {
                                "bytes_received": "500",
                                "bytes_transmitted": "1000",
                                "packets_received": "5",
                                "packets_transmitted": "10",
                            },
                        }
                    },
                ),
            )
        )
        client, target = _client(api)
        r = fetch_interfaces_detailed(client, target, "k", "s")
        wan = r.interfaces[0]
        assert wan.bytes_received == 500
        assert wan.bytes_transmitted == 1000
        assert wan.packets_transmitted == 10

    def test_traffic_non_numeric_falls_back_to_zero(self) -> None:
        # OPNsense-Response mit Muell in den Zahlen darf nicht crashen.
        api = (
            MockApi()
            .on(
                INTERFACES_INFO_ENDPOINT,
                lambda _req: httpx.Response(
                    200,
                    json={
                        "rows": [
                            {"identifier": "wan", "device": "em0", "enabled": True},
                        ]
                    },
                ),
            )
            .on(
                INTERFACES_TRAFFIC_ENDPOINT,
                lambda _req: httpx.Response(
                    200,
                    json={
                        "interfaces": {
                            "em0": {
                                "bytes received": "not-a-number",
                                "bytes transmitted": None,
                            },
                        }
                    },
                ),
            )
        )
        client, target = _client(api)
        r = fetch_interfaces_detailed(client, target, "k", "s")
        assert r.interfaces[0].bytes_received == 0
        assert r.interfaces[0].bytes_transmitted == 0
