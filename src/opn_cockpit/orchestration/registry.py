"""Subsystem-Registry: Name → ``(Adapter, Controller)``.

Hält in genau einer Stelle die Zuordnung zwischen dem Klartext-Namen eines
Subsystems (wie er im Plan-File und im Audit-Log erscheint) und der konkreten
Implementierung. Spätere Schritte (Aliasse, Unbound-DNS, Firewall-Regeln)
fügen hier einen Eintrag hinzu — Orchestrierung und CLI berühren das nicht.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opn_cockpit.core.objects.aliases import AliasAdapter, AliasesController
from opn_cockpit.core.objects.base import ObjectAdapter, SubsystemController
from opn_cockpit.core.objects.firewall_rules import RuleAdapter, RulesController
from opn_cockpit.core.objects.routes import RouteAdapter, RoutesController


@dataclass(frozen=True, slots=True)
class SubsystemBinding:
    """Bündel aus Adapter + Controller für ein Subsystem.

    Adapter trägt die per-Objekt-Operationen, Controller das
    pro-Gerät-einmalige ``reconfigure``.
    """

    name: str
    adapter: ObjectAdapter[Any, Any]
    controller: SubsystemController


ROUTES = SubsystemBinding(
    name="routes",
    adapter=RouteAdapter(),
    controller=RoutesController(),
)

FIREWALL_ALIAS = SubsystemBinding(
    name="firewall_alias",
    adapter=AliasAdapter(),
    controller=AliasesController(),
)

FIREWALL_RULES = SubsystemBinding(
    name="firewall_rules",
    adapter=RuleAdapter(),
    controller=RulesController(),
)

_REGISTRY: dict[str, SubsystemBinding] = {
    ROUTES.name: ROUTES,
    FIREWALL_ALIAS.name: FIREWALL_ALIAS,
    FIREWALL_RULES.name: FIREWALL_RULES,
}


def get_binding(subsystem: str) -> SubsystemBinding:
    """Liefert das Binding für ein Subsystem.

    Wirft ``KeyError``, wenn ``subsystem`` unbekannt ist — die CLI fängt
    das ab und meldet "Aktion in dieser Version nicht unterstützt".
    """
    return _REGISTRY[subsystem]


def known_subsystems() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY.keys()))
