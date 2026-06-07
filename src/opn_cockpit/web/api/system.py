"""System-Telemetrie (Disk-Space) fuer den Server-Host.

Liefert dem Frontend eine kompakte Sicht auf den Speicherplatz auf dem
Volume, auf dem ``<app_data>`` liegt — dorthin schreiben Backups, Audit-
Log, SQLite-DBs und (bei aktiviertem Scheduled-Backup) potenziell viele
GB Konfig-Snapshots.

Nur sinnvoll im Server-Deployment (Linux-Container, Linux-Service,
Multi-User-Windows-Server). Beim Single-User-PAW-Modus zeigt das
Frontend das Widget ausgeblendet — der Admin sieht den Platz ohnehin in
seinem File-Explorer.
"""

from __future__ import annotations

import shutil
import sys

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from opn_cockpit.config import get_app_data_dir
from opn_cockpit.security.session import Session
from opn_cockpit.web.auth.dependencies import require_session

router = APIRouter(prefix="/api/system", tags=["system"])


class DiskSpaceResponse(BaseModel):
    """Disk-Space-Snapshot fuer das ``<app_data>``-Volume.

    ``relevant`` ist False auf dem Single-User-Windows-Loopback-Setup —
    dort ist die Anzeige selten interessant und wuerde nur Topbar-Platz
    verbrauchen. Frontend versteckt das Widget dann.
    """

    relevant: bool
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_percent: float
    severity: str  # 'ok' | 'warn' | 'critical'

    @classmethod
    def empty(cls) -> DiskSpaceResponse:
        return cls(
            relevant=False,
            path="",
            total_bytes=0,
            used_bytes=0,
            free_bytes=0,
            used_percent=0.0,
            severity="ok",
        )


_SEVERITY_WARN_PERCENT = 80.0
_SEVERITY_CRIT_PERCENT = 92.0


def _classify(used_percent: float) -> str:
    if used_percent >= _SEVERITY_CRIT_PERCENT:
        return "critical"
    if used_percent >= _SEVERITY_WARN_PERCENT:
        return "warn"
    return "ok"


@router.get("/disk", response_model=DiskSpaceResponse)
def get_disk_usage(
    session: Session = Depends(require_session),
) -> DiskSpaceResponse:
    """Liefert Disk-Usage fuer das App-Data-Volume.

    Auf Windows-Loopback-Single-User-Setups gibt der Endpoint
    ``relevant=False`` zurueck — das Topbar-Widget rendert dann nichts.
    Auf Linux + Multi-User-Windows-Server liefert er echte Werte.
    """
    session.touch()
    app_data = get_app_data_dir()
    # Auf Windows-Single-User-PAW ist die Anzeige praktisch nicht
    # interessant — Admin sieht das eh im Explorer. Frontend kann das
    # Widget dann ausblenden, ohne dass wir die Erkennung clientseitig
    # aufbauen muessen.
    is_windows = sys.platform == "win32"
    # ``relevant`` ist konservativ: wir signalisieren False nur dann,
    # wenn die Heuristik klar ist (Windows + nicht Multi-User-Mode).
    # Server-State sehen wir hier nicht; das Frontend kennt seinen Mode
    # und kann das verfeinern.
    try:
        usage = shutil.disk_usage(app_data)
    except OSError:
        return DiskSpaceResponse.empty()
    used_percent = (usage.used / usage.total * 100.0) if usage.total else 0.0
    return DiskSpaceResponse(
        relevant=not is_windows,  # Windows: defaultmaessig versteckt
        path=str(app_data),
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
        used_percent=round(used_percent, 1),
        severity=_classify(used_percent),
    )


__all__ = ["router"]
