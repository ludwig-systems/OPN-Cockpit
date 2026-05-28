"""API-Discovery: vorhandene Gateways und Aliase pro Gerät auflisten.

v1.1-Feature, das Fehleingaben in den Action-Dialogen reduziert: Statt
freihändig einen Gateway-Namen zu tippen (z. B. ``v2_wanbwin`` statt
``V2_WANBwIn`` — wäre still wrong, weil Gateway-Namen case-sensitive sind),
fragt das Tool die Liste der vorhandenen Namen über die OPNsense-API ab
und bietet sie als Auswahl an.

Die Funktionen sind **read-only** und ohne Side-Effects auf der Box —
sie taugen auch für GUI-Auto-Suggest und CLI-Discovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opn_cockpit.core.errors import OpnCockpitError
from opn_cockpit.core.http_client import HttpClient, HttpTarget
from opn_cockpit.core.objects._endpoints import ALIAS_SEARCH, GATEWAY_STATUS

# ---------------------------------------------------------------------------
# Datentypen
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GatewaySummary:
    """Übersichtseintrag eines konfigurierten Gateways."""

    name: str
    address: str = ""
    status: str = ""

    @property
    def is_online(self) -> bool:
        return self.status.lower() in {"online", "up", "ok", ""}


@dataclass(frozen=True, slots=True)
class AliasSummary:
    """Übersichtseintrag eines bestehenden Alias."""

    name: str
    type: str = ""
    descr: str = ""


class DiscoveryError(OpnCockpitError):
    """Discovery-Aufruf gegen die OPNsense ist gescheitert."""

    default_kind = "discovery"


# ---------------------------------------------------------------------------
# Gateways
# ---------------------------------------------------------------------------


def list_gateways(
    client: HttpClient,
    target: HttpTarget,
    key: str,
    secret: str,
) -> list[GatewaySummary]:
    """Liefert alle auf dem Gerät konfigurierten Gateway-Namen.

    Endpoint: ``GET /api/routes/gateway/status`` (Standard OPNsense-26.x).
    Antwortformat (üblich): ``{"items": [{"name": "WAN_GW", "address": "1.2.3.4",
    "status": "online"}, ...]}``. Wir lesen defensiv:

    * fehlende Felder werden zu Leerstrings,
    * unbekannte Wurzel- oder Item-Formen liefern eine leere Liste.

    Wirft :class:`DiscoveryError` nur, wenn der HTTP-Aufruf selbst fehlschlägt
    (Netzwerk/Auth). API-Schema-Drift führt nicht zur Exception, sondern zu
    einer leeren Liste — der Aufrufer (GUI) kann dann auf freien Text-Input
    zurückfallen.
    """
    try:
        response = client.call(target, key, secret, "GET", GATEWAY_STATUS)
    except OpnCockpitError as exc:
        raise DiscoveryError(
            f"Gateway-Liste nicht abrufbar von {target.host}.",
            context=exc.context,
        ) from exc
    try:
        data: Any = response.json()
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("items") or data.get("rows") or []
    if not isinstance(items, list):
        return []
    result: list[GatewaySummary] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        if not name:
            continue
        result.append(
            GatewaySummary(
                name=name,
                address=str(raw.get("address", "")),
                status=str(raw.get("status", "")),
            )
        )
    return sorted(result, key=lambda g: g.name.lower())


# ---------------------------------------------------------------------------
# Aliase
# ---------------------------------------------------------------------------


def list_aliases(
    client: HttpClient,
    target: HttpTarget,
    key: str,
    secret: str,
) -> list[AliasSummary]:
    """Liefert alle bestehenden Alias-Namen + Typen auf dem Gerät.

    Endpoint: ``POST /api/firewall/alias/searchItem`` ohne Filter.
    Antwortformat: ``{"rows": [{"uuid": "...", "name": "...", "type": "..."}], ...}``.
    Defensiv: leere Liste bei Schema-Drift.
    """
    try:
        response = client.call(
            target, key, secret,
            "POST", ALIAS_SEARCH,
            json={"current": 1, "rowCount": -1, "searchPhrase": ""},
        )
    except OpnCockpitError as exc:
        raise DiscoveryError(
            f"Alias-Liste nicht abrufbar von {target.host}.",
            context=exc.context,
        ) from exc
    try:
        data: Any = response.json()
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    rows = data.get("rows") or data.get("items") or []
    if not isinstance(rows, list):
        return []
    result: list[AliasSummary] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        if not name:
            continue
        result.append(
            AliasSummary(
                name=name,
                type=str(raw.get("type", "")),
                descr=str(raw.get("description") or raw.get("descr") or ""),
            )
        )
    return sorted(result, key=lambda a: a.name.lower())
