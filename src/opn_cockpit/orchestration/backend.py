"""Plan-Store-Backend-Interface + Factory.

Analog zu :mod:`opn_cockpit.audit.backend` — Aufrufer sprechen mit dem
Protocol, die Factory liefert heute die File-Implementierung
(:class:`PlanStore` aus :mod:`opn_cockpit.orchestration.plan_store`).
In v3 kommt ein SQL-Backend dazu.

Speichert:

* Plan-Files (``{plan_id}.json``)
* Apply-Reports (``{plan_id}.report.json``)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from opn_cockpit.config import get_app_data_dir
from opn_cockpit.orchestration.plan_store import PlanStore

if TYPE_CHECKING:
    from pathlib import Path

    from opn_cockpit.core.result import RolloutReport
    from opn_cockpit.orchestration.planner import Plan


@runtime_checkable
class PlanStoreBackend(Protocol):
    """Pflichtschnittstelle aller Plan-Store-Backends."""

    def save(self, plan: Plan) -> Path:
        ...

    def load(self, plan_id_or_path: str) -> Plan:
        ...

    def list_ids(self) -> list[str]:
        ...

    def save_report(self, plan_id: str, report: RolloutReport) -> Path:
        ...

    def load_report(self, plan_id: str) -> RolloutReport | None:
        ...

    def delete(self, plan_id: str) -> bool:
        ...


def get_plan_store_backend() -> PlanStoreBackend:
    """Liefert das aktuell konfigurierte Plan-Store-Backend.

    Heute: immer File-basierter ``PlanStore`` unter
    ``$APP_DATA_DIR/plans/``. In v3 wahlweise SqlPlanStoreBackend.
    """
    return PlanStore(base_dir=get_app_data_dir() / "plans")


__all__ = ["PlanStoreBackend", "get_plan_store_backend"]
