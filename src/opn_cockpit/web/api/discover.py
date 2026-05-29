"""Discovery-Routen: Gateway- und Alias-Namen pro Geraet auflisten.

Wrappt ``core.discovery``. Wird vom Frontend genutzt, um die Plan-Modal-
Felder mit Auto-Suggest zu befuellen — case-sensitive Tippfehler bei
Gateway-Namen werden so seltener.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from opn_cockpit.core.discovery import (
    DiscoveryError,
    list_aliases,
    list_gateways,
)
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning
from opn_cockpit.security.session import Session
from opn_cockpit.vault.model import VaultDevice
from opn_cockpit.web.api.schemas import (
    AliasDiscoveryResponse,
    AliasSummaryResponse,
    GatewayDiscoveryResponse,
    GatewaySummaryResponse,
)
from opn_cockpit.web.auth.dependencies import require_session

router = APIRouter(prefix="/api/discover", tags=["discover"])


@router.get(
    "/devices/{device_id}/gateways",
    response_model=GatewayDiscoveryResponse,
)
def discover_gateways(
    device_id: str,
    session: Session = Depends(require_session),
) -> GatewayDiscoveryResponse:
    """Liefert alle Gateway-Namen, die auf der OPNsense konfiguriert sind."""
    device = _find_device(session, device_id)
    target, tuning = _target_and_tuning(device, session)
    with HttpClient(targets=[target], tuning=tuning) as client:
        try:
            gateways = list_gateways(client, target, device.api_key, device.api_secret)
        except DiscoveryError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=exc.context.summary or "Gateway-Discovery fehlgeschlagen.",
            ) from exc
    session.touch()
    return GatewayDiscoveryResponse(
        device_id=device_id,
        gateways=[
            GatewaySummaryResponse(name=g.name, address=g.address, status=g.status)
            for g in gateways
        ],
    )


@router.get(
    "/devices/{device_id}/aliases",
    response_model=AliasDiscoveryResponse,
)
def discover_aliases(
    device_id: str,
    session: Session = Depends(require_session),
) -> AliasDiscoveryResponse:
    """Liefert alle bestehenden Alias-Namen auf der OPNsense."""
    device = _find_device(session, device_id)
    target, tuning = _target_and_tuning(device, session)
    with HttpClient(targets=[target], tuning=tuning) as client:
        try:
            aliases = list_aliases(client, target, device.api_key, device.api_secret)
        except DiscoveryError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=exc.context.summary or "Alias-Discovery fehlgeschlagen.",
            ) from exc
    session.touch()
    return AliasDiscoveryResponse(
        device_id=device_id,
        aliases=[
            AliasSummaryResponse(name=a.name, type=a.type, descr=a.descr)
            for a in aliases
        ],
    )


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


def _find_device(session: Session, device_id: str) -> VaultDevice:
    for d in session.opened.data.devices:
        if d.id == device_id:
            return d
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Geraet mit ID '{device_id}' nicht im Tresor.",
    )


def _target_and_tuning(
    device: VaultDevice, session: Session,
) -> tuple[HttpTarget, HttpTuning]:
    target = HttpTarget(
        host=device.host, port=device.port, verify=device.tls_verify,
    )
    s = session.opened.data.settings
    tuning = HttpTuning(
        connect_timeout_s=s.connect_timeout_s,
        read_timeout_s=s.read_timeout_s,
        reconfigure_timeout_s=s.reconfigure_timeout_s,
        retry_count=s.retry_count,
    )
    return target, tuning


__all__ = ["router"]
