"""Formatierung von Plan und RolloutReport für CLI-Ausgabe und Audit.

Reine Darstellungs-Helfer. Keine I/O, keine Seiteneffekte — die
Konsum-Schicht (CLI, GUI) entscheidet, wohin die Strings gehen.
"""

from __future__ import annotations

from opn_cockpit.core.result import Result, RolloutReport, Status
from opn_cockpit.orchestration.planner import Plan, PlannedDeviceAction

# ---------------------------------------------------------------------------
# Plan-Vorschau
# ---------------------------------------------------------------------------


def format_plan_summary(plan: Plan) -> str:
    """Einzeilige Kurzfassung — passt in eine Log-Zeile oder Statusleiste."""
    return (
        f"Plan {plan.plan_id} ({plan.action}): "
        f"{plan.target_count} Ziel(e), "
        f"{plan.to_apply_count} schreiben, "
        f"{plan.skip_count} überspringen."
    )


def format_plan_preview(plan: Plan) -> str:
    """Mehrzeilige Vorschau für die CLI vor der Bestätigung (R-PRE-1)."""
    lines: list[str] = []
    lines.append(f"=== Plan {plan.plan_id} ===")
    lines.append(f"Aktion:    {plan.action}")
    lines.append(f"Subsystem: {plan.subsystem}")
    lines.append(f"Erstellt:  {plan.created_at_utc}")
    lines.append(f"Ziele:     {plan.target_count} "
                 f"(schreiben: {plan.to_apply_count}, überspringen: {plan.skip_count})")
    lines.append("")
    lines.append("Geräte:")
    for action in plan.actions:
        lines.append(_format_planned_action(action))
    return "\n".join(lines)


def _format_planned_action(action: PlannedDeviceAction) -> str:
    badge = action.diff.kind.value.upper().ljust(6)
    label = f"{action.device.name} ({action.device.host}:{action.device.port})"
    tls = "" if action.device.tls_verify else "  [TLS-Verify AUS — Risiko]"
    payload = _format_dict_inline(action.payload_masked)
    return (
        f"  [{badge}] {label}{tls}\n"
        f"           Diff:    {action.diff.summary}\n"
        f"           Payload: {payload}"
    )


def _format_dict_inline(d: dict[str, object]) -> str:
    parts = [f"{k}={v!r}" for k, v in d.items()]
    return "{ " + ", ".join(parts) + " }"


# ---------------------------------------------------------------------------
# Rollout-Report
# ---------------------------------------------------------------------------


_STATUS_BADGE: dict[Status, str] = {
    Status.VERIFIED: "OK",
    Status.SKIPPED: "SKIP",
    Status.WRITTEN: "TEIL",
    Status.ACTIVATED: "TEIL",
    Status.FAILED: "FAIL",
}


def format_rollout_summary(report: RolloutReport) -> str:
    return (
        f"{report.successes}/{report.total} ok, "
        f"{report.failures} fehlgeschlagen, "
        f"{report.skipped} übersprungen"
    )


def format_rollout_matrix(
    report: RolloutReport,
    *,
    devices_by_id: dict[str, str] | None = None,
) -> str:
    """Tabellenförmige Auflistung für die CLI nach Abschluss eines Apply.

    ``devices_by_id`` ist optional und bildet Geräte-ID auf einen Anzeige-
    namen ab (sonst wird die ID selbst angezeigt). Wir akzeptieren die
    Information separat, weil ``Result`` aus Schicht-1-Reinheitsgründen
    keinen Anzeigenamen mitführt.
    """
    devices_by_id = devices_by_id or {}
    if not report.results:
        return "(keine Ergebnisse)"

    lines: list[str] = []
    header = f"{'Gerät':<30} {'Status':<8} {'Phase':<10} {'ms':>6}  Hinweis"
    lines.append(header)
    lines.append("-" * len(header))
    for result in report.results:
        name = devices_by_id.get(result.device_id, result.device_id)
        badge = _STATUS_BADGE.get(result.status, "?")
        phase = _format_phase(result)
        duration = result.duration_ms
        msg = result.short_message
        lines.append(
            f"{name[:30]:<30} {badge:<8} {phase:<10} {duration:>6}  {msg}"
        )
    lines.append("")
    lines.append("Gesamt: " + format_rollout_summary(report))
    return "\n".join(lines)


def _format_phase(result: Result) -> str:
    if result.status is Status.VERIFIED:
        return "verify"
    if result.status is Status.SKIPPED:
        return "-"
    if result.failed_phase is None:
        return ""
    return result.failed_phase.value
