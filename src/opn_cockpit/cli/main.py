"""Headless-CLI für OPN-Cockpit.

Erfüllt den Demo-Meilenstein aus Schritt 6 des Umsetzungsplans:

* ``create-vault`` — neuen Tresor anlegen
* ``add-device`` / ``remove-device`` / ``list-devices`` — Inventar pflegen
* ``test-connection`` — Erreichbarkeit + Auth gegen Geräte prüfen
* ``plan add-route`` — Vorschau erzeugen + persistieren
* ``apply`` — Plan ausrollen (Schreiben + Reconfigure + Read-back)
* ``audit`` — Audit-Log filtern und anzeigen
* ``change-password`` / ``export-template`` — Tresor-Wartung

Jeder Sub-Command läuft als eigenständiger Prozess: Master-Passwort wird
abgefragt, der Tresor wird entsperrt, der Befehl wird ausgeführt, das
Programm endet. (Die persistente Inaktivitäts-Session ist Sache der GUI
in Schritt 8.)

Konsens des Tools (alle Sub-Commands):
* Alle vault-relevanten Operationen schreiben ins Audit-Log.
* Apply zeigt VOR der Ausführung erneut die Vorschau und verlangt
  explizites ``ja`` als Bestätigung (R-PRE-2).
* Klartext-Secrets werden in Plan-Dateien, Audit-Log und CLI-Ausgabe
  nicht durchgereicht — Maskierung passiert in Planner / Audit / Reporter.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from opn_cockpit.audit.log import AuditEventKind, AuditLog, default_audit_path
from opn_cockpit.cli._io import (
    confirm,
    emit,
    prompt_password,
    prompt_password_with_confirmation,
    resolve_vault_path,
)
from opn_cockpit.config import AppSettings, get_app_data_dir
from opn_cockpit.core.health import check_device
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning
from opn_cockpit.core.objects.aliases import AliasSpec
from opn_cockpit.core.objects.routes import RouteSpec
from opn_cockpit.inventory.store import InventoryStore
from opn_cockpit.orchestration.executor import Executor
from opn_cockpit.orchestration.plan_store import PlanStore, PlanStoreError
from opn_cockpit.orchestration.planner import Planner
from opn_cockpit.orchestration.registry import get_binding
from opn_cockpit.orchestration.reporter import (
    format_plan_preview,
    format_plan_summary,
    format_rollout_matrix,
)
from opn_cockpit.profiles.store import (
    ProfileStore,
    ProfileStoreError,
    default_profiles_path,
)
from opn_cockpit.security.session import Session
from opn_cockpit.vault.errors import (
    InvalidPasswordError,
    UnknownDeviceError,
    VaultError,
    WeakPasswordError,
)
from opn_cockpit.vault.model import VaultDevice
from opn_cockpit.vault.store import (
    change_password,
    create_vault,
    export_template,
    open_vault,
    save_vault,
)

# Exit-Codes
EXIT_OK = 0
EXIT_GENERAL_ERROR = 1
EXIT_USER_ABORT = 2
EXIT_AUTH_ERROR = 3
EXIT_VAULT_ERROR = 4
EXIT_NETWORK_ERROR = 5


# ===========================================================================
# Parser-Aufbau
# ===========================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opn-cockpit-cli",
        description="OPN-Cockpit Headless-CLI.",
    )
    parser.add_argument(
        "--vault",
        help="Pfad zur Tresor-Datei (.opnvault). Überschreibt Default aus settings.json.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=False)

    # ----- Tresor-Wartung -----

    p_create = sub.add_parser("create-vault", help="Neuen Tresor anlegen")
    p_create.add_argument("path", help="Pfad der neuen Tresor-Datei")

    sub.add_parser("change-password", help="Master-Passwort eines Tresors ändern")

    p_export = sub.add_parser(
        "export-template",
        help="Tresor als Template (mit leeren Secret-Feldern) exportieren",
    )
    p_export.add_argument("dest", help="Ziel-Pfad der Template-Datei")

    # ----- Inventar -----

    sub.add_parser("list-devices", help="Inventarisierte Geräte auflisten")

    p_add = sub.add_parser("add-device", help="Gerät zum Tresor hinzufügen")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--host", required=True)
    p_add.add_argument("--port", type=int, default=443)
    p_add.add_argument(
        "--tls-verify", dest="tls_verify", action="store_true", default=True,
        help="TLS-Zertifikat prüfen (Default)",
    )
    p_add.add_argument(
        "--no-tls-verify", dest="tls_verify", action="store_false",
        help="TLS-Verifikation deaktivieren (Risiko — wird im UI markiert)",
    )
    p_add.add_argument(
        "--tags", default="",
        help="Komma-separierte Tags, z. B. 'branches,germany'",
    )
    p_add.add_argument("--descr", default="")

    p_rm = sub.add_parser("remove-device", help="Gerät aus dem Tresor entfernen")
    p_rm.add_argument("--id", required=True, dest="device_id")

    # ----- Verbindungstest -----

    p_test = sub.add_parser("test-connection", help="Erreichbarkeit + Auth eines Geräts prüfen")
    p_test.add_argument(
        "--target", default="all",
        help="Selektor: 'all', 'tag:X', 'group:X', 'id:X', 'name:X' "
             "(siehe Inventar-Selektoren)",
    )

    # ----- Plan -----

    p_plan = sub.add_parser("plan", help="Aktions-Vorschau erzeugen")
    p_plan_sub = p_plan.add_subparsers(dest="plan_action", required=True)
    p_plan_route = p_plan_sub.add_parser("add-route", help="Plan für neue Route(n)")
    p_plan_route.add_argument("--network", required=True, help="CIDR-Netz, z. B. 10.99.0.0/24")
    p_plan_route.add_argument("--gateway", required=True, help="Gateway-Name (case-sensitive)")
    p_plan_route.add_argument("--descr", default="")
    p_plan_route.add_argument("--disabled", action="store_true")
    p_plan_route.add_argument("--target", default="all", help="Geräte-Selektor")

    p_plan_alias = p_plan_sub.add_parser("add-alias", help="Plan für neuen Alias")
    p_plan_alias.add_argument("--name", required=True)
    p_plan_alias.add_argument(
        "--type", required=True, dest="alias_type",
        help="host, network, port, url, ...",
    )
    p_plan_alias.add_argument(
        "--content", required=True,
        help="Komma-separierte Einträge, z. B. '10.0.0.1,10.0.0.2'",
    )
    p_plan_alias.add_argument("--descr", default="")
    p_plan_alias.add_argument("--target", default="all", help="Geräte-Selektor")

    p_plan_append = p_plan_sub.add_parser(
        "append-alias",
        help="Plan für Erweiterung eines bestehenden Alias (Merge)",
    )
    p_plan_append.add_argument("--name", required=True)
    p_plan_append.add_argument(
        "--type", required=True, dest="alias_type",
        help="host, network, port, url, ... (für Plan-Vorschau)",
    )
    p_plan_append.add_argument(
        "--content", required=True,
        help="Komma-separierte Einträge zum Anhängen",
    )
    p_plan_append.add_argument("--target", default="all", help="Geräte-Selektor")

    # ----- Apply -----

    p_apply = sub.add_parser("apply", help="Vorher erzeugte Vorschau ausrollen")
    p_apply.add_argument("plan", help="Plan-ID (pl-XXXXXXXX) oder Pfad zu einer Plan-Datei")

    # ----- Audit -----

    p_profile = sub.add_parser("profile", help="Aktions-Profile (Templates) verwalten")
    p_profile_sub = p_profile.add_subparsers(dest="profile_action", required=True)
    p_profile_sub.add_parser("list", help="Gespeicherte Profile auflisten")
    p_profile_delete = p_profile_sub.add_parser("delete", help="Profil löschen")
    p_profile_delete.add_argument("profile_id")
    p_profile_apply = p_profile_sub.add_parser(
        "apply", help="Profil laden und sofort ausrollen",
    )
    p_profile_apply.add_argument("profile_id_or_name")
    p_profile_apply.add_argument(
        "--target", default=None,
        help="Selektor überschreiben (Default: aus dem Profil)",
    )
    p_profile_save = p_profile_sub.add_parser(
        "save-route", help="Routen-Aktion als Profil speichern",
    )
    p_profile_save.add_argument("--name", required=True, dest="profile_name")
    p_profile_save.add_argument("--network", required=True)
    p_profile_save.add_argument("--gateway", required=True)
    p_profile_save.add_argument("--descr", default="")
    p_profile_save.add_argument("--disabled", action="store_true")
    p_profile_save.add_argument("--target", default="all")

    p_profile_save_alias = p_profile_sub.add_parser(
        "save-alias", help="Alias-Aktion als Profil speichern",
    )
    p_profile_save_alias.add_argument("--name", required=True, dest="profile_name")
    p_profile_save_alias.add_argument("--alias-name", required=True, dest="alias_name")
    p_profile_save_alias.add_argument("--type", required=True, dest="alias_type")
    p_profile_save_alias.add_argument("--content", required=True)
    p_profile_save_alias.add_argument("--descr", default="")
    p_profile_save_alias.add_argument(
        "--append", action="store_true",
        help="Profil als append/merge speichern (Default: create)",
    )
    p_profile_save_alias.add_argument("--target", default="all")

    p_audit = sub.add_parser("audit", help="Audit-Log filtern und anzeigen")
    p_audit.add_argument("--event")
    p_audit.add_argument("--action")
    p_audit.add_argument("--device-id", dest="target_device_id")
    p_audit.add_argument("--actor")
    p_audit.add_argument("--since", dest="since_iso")
    p_audit.add_argument("--until", dest="until_iso")
    p_audit.add_argument("--limit", type=int, default=50)

    return parser


# ===========================================================================
# Entry Point
# ===========================================================================


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_GENERAL_ERROR

    settings = AppSettings.load()

    try:
        return _dispatch(args, settings)
    except KeyboardInterrupt:
        emit("\nAbbruch durch Benutzer.", err=True)
        return EXIT_USER_ABORT


def _dispatch(args: argparse.Namespace, settings: AppSettings) -> int:
    handlers_without_settings: dict[str, Callable[[argparse.Namespace], int]] = {
        "create-vault": cmd_create_vault,
        "audit": cmd_audit,
    }
    handlers_with_settings: dict[str, Callable[[argparse.Namespace, AppSettings], int]] = {
        "change-password": cmd_change_password,
        "export-template": cmd_export_template,
        "list-devices": cmd_list_devices,
        "add-device": cmd_add_device,
        "remove-device": cmd_remove_device,
        "test-connection": cmd_test_connection,
        "plan": cmd_plan,
        "apply": cmd_apply,
        "profile": cmd_profile,
    }
    cmd = args.command
    if cmd in handlers_without_settings:
        return handlers_without_settings[cmd](args)
    if cmd in handlers_with_settings:
        return handlers_with_settings[cmd](args, settings)
    emit(f"Unbekannter Befehl: {cmd}", err=True)
    return EXIT_GENERAL_ERROR


# ===========================================================================
# Plan-Factories
# ===========================================================================


def _route_plan_from_args(args: argparse.Namespace) -> tuple[str, str, RouteSpec]:
    spec = RouteSpec(
        network=args.network,
        gateway=args.gateway,
        descr=args.descr,
        disabled=bool(args.disabled),
    )
    return ("add_route", "routes", spec)


def _alias_plan_from_args(args: argparse.Namespace) -> tuple[str, str, AliasSpec]:
    content = tuple(c.strip() for c in args.content.split(",") if c.strip())
    if not content:
        raise ValueError("Mindestens ein Alias-Eintrag erforderlich (--content).")
    spec = AliasSpec(
        name=args.name,
        type=args.alias_type,
        content=content,
        descr=getattr(args, "descr", ""),
        merge_mode="create",
    )
    return ("add_alias", "firewall_alias", spec)


def _alias_append_plan_from_args(args: argparse.Namespace) -> tuple[str, str, AliasSpec]:
    content = tuple(c.strip() for c in args.content.split(",") if c.strip())
    if not content:
        raise ValueError("Mindestens ein Alias-Eintrag erforderlich (--content).")
    spec = AliasSpec(
        name=args.name,
        type=args.alias_type,
        content=content,
        descr="",
        merge_mode="append",
    )
    return ("append_alias", "firewall_alias", spec)


_PLAN_FACTORIES: dict[str, Callable[[argparse.Namespace], tuple[str, str, object]]] = {
    "add-route": _route_plan_from_args,
    "add-alias": _alias_plan_from_args,
    "append-alias": _alias_append_plan_from_args,
}


# ===========================================================================
# Sub-Commands
# ===========================================================================


def cmd_create_vault(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser()
    if path.exists():
        emit(f"Datei existiert bereits: {path}", err=True)
        return EXIT_GENERAL_ERROR
    try:
        pw = prompt_password_with_confirmation("Neues Master-Passwort (min. 12 Zeichen)")
    except ValueError as exc:
        emit(str(exc), err=True)
        return EXIT_USER_ABORT
    try:
        create_vault(path, pw)
    except WeakPasswordError as exc:
        emit(str(exc), err=True)
        return EXIT_VAULT_ERROR
    audit = AuditLog(path=default_audit_path())
    audit.append(
        AuditEventKind.VAULT_CREATED,
        vault_path=str(path),
        summary=f"Neuer Tresor angelegt: {path}",
    )
    settings = AppSettings.load()
    settings.remember_vault(path)
    if settings.default_vault is None:
        settings.default_vault = str(path)
    settings.save()
    emit(f"Tresor angelegt: {path}")
    return EXIT_OK


def cmd_change_password(args: argparse.Namespace, settings: AppSettings) -> int:
    path = _resolve_vault(args, settings)
    old_pw = prompt_password(f"Aktuelles Master-Passwort für {path}")
    try:
        new_pw = prompt_password_with_confirmation("Neues Master-Passwort (min. 12 Zeichen)")
    except ValueError as exc:
        emit(str(exc), err=True)
        return EXIT_USER_ABORT
    try:
        change_password(path, old_pw, new_pw)
    except InvalidPasswordError:
        _audit_login_failed(path, "change-password")
        emit("Aktuelles Passwort falsch.", err=True)
        return EXIT_AUTH_ERROR
    except WeakPasswordError as exc:
        emit(str(exc), err=True)
        return EXIT_VAULT_ERROR
    audit = AuditLog(path=default_audit_path())
    audit.append(
        AuditEventKind.VAULT_PASSWORD_CHANGED,
        vault_path=str(path),
        summary=f"Master-Passwort geändert: {path}",
    )
    emit("Master-Passwort geändert.")
    return EXIT_OK


def cmd_export_template(args: argparse.Namespace, settings: AppSettings) -> int:
    path = _resolve_vault(args, settings)
    dest = Path(args.dest).expanduser()
    pw = prompt_password(f"Master-Passwort für {path}")
    try:
        export_template(path, dest, pw)
    except InvalidPasswordError:
        _audit_login_failed(path, "export-template")
        emit("Master-Passwort falsch.", err=True)
        return EXIT_AUTH_ERROR
    except VaultError as exc:
        emit(str(exc), err=True)
        return EXIT_VAULT_ERROR
    audit = AuditLog(path=default_audit_path())
    audit.append(
        AuditEventKind.TEMPLATE_EXPORTED,
        vault_path=str(dest),
        summary=f"Template exportiert: {dest} (Quelle: {path})",
    )
    emit(f"Template exportiert: {dest}")
    return EXIT_OK


def cmd_list_devices(args: argparse.Namespace, settings: AppSettings) -> int:
    session = _unlock_for_command(args, settings)
    if session is None:
        return EXIT_AUTH_ERROR
    store = InventoryStore(session=session)
    devices = store.list_devices()
    if not devices:
        emit("(keine Geräte im Inventar)")
        return EXIT_OK
    emit(f"{'ID':<38} {'Name':<24} {'Host':<32} {'TLS':<4} Tags")
    emit("-" * 110)
    for d in devices:
        tls = "ja" if d.tls_verify else "AUS"
        tags = ",".join(d.tags)
        emit(f"{d.id:<38} {d.name[:24]:<24} {d.host[:32]:<32} {tls:<4} {tags}")
    return EXIT_OK


def cmd_add_device(args: argparse.Namespace, settings: AppSettings) -> int:
    session = _unlock_for_command(args, settings)
    if session is None:
        return EXIT_AUTH_ERROR
    vault_path = session.vault_path
    assert vault_path is not None
    api_key = prompt_password(f"API-Key für {args.name}")
    api_secret = prompt_password(f"API-Secret für {args.name}")
    new_device = VaultDevice(
        id=VaultDevice.new_id(),
        name=args.name,
        host=args.host,
        port=args.port,
        tls_verify=args.tls_verify,
        tags=[t.strip() for t in args.tags.split(",") if t.strip()],
        api_key=api_key,
        api_secret=api_secret,
        descr=args.descr,
    )
    session.opened.data.devices.append(new_device)
    pw = prompt_password(f"Master-Passwort zum Speichern in {vault_path}")
    try:
        new_opened = save_vault(vault_path, session.opened, pw)
    except InvalidPasswordError:
        emit("Master-Passwort falsch — Änderung nicht gespeichert.", err=True)
        return EXIT_AUTH_ERROR
    session.replace_opened(new_opened)
    emit(f"Gerät '{args.name}' hinzugefügt (ID: {new_device.id}).")
    return EXIT_OK


def cmd_remove_device(args: argparse.Namespace, settings: AppSettings) -> int:
    session = _unlock_for_command(args, settings)
    if session is None:
        return EXIT_AUTH_ERROR
    vault_path = session.vault_path
    assert vault_path is not None
    devices = session.opened.data.devices
    matches = [d for d in devices if d.id == args.device_id]
    if not matches:
        emit(f"Gerät mit ID '{args.device_id}' nicht gefunden.", err=True)
        return EXIT_GENERAL_ERROR
    session.opened.data.devices = [d for d in devices if d.id != args.device_id]
    pw = prompt_password(f"Master-Passwort zum Speichern in {vault_path}")
    try:
        new_opened = save_vault(vault_path, session.opened, pw)
    except InvalidPasswordError:
        emit("Master-Passwort falsch — Änderung nicht gespeichert.", err=True)
        return EXIT_AUTH_ERROR
    session.replace_opened(new_opened)
    emit(f"Gerät '{matches[0].name}' entfernt.")
    return EXIT_OK


def cmd_test_connection(args: argparse.Namespace, settings: AppSettings) -> int:
    session = _unlock_for_command(args, settings)
    if session is None:
        return EXIT_AUTH_ERROR
    store = InventoryStore(session=session)
    try:
        devices = store.select(args.target)
    except Exception as exc:
        emit(f"Selektor ungültig: {exc}", err=True)
        return EXIT_GENERAL_ERROR
    if not devices:
        emit(f"Keine Geräte für Selektor '{args.target}' gefunden.")
        return EXIT_OK
    tuning = _tuning_from_session(session)
    targets = [HttpTarget(host=d.host, port=d.port, verify=d.tls_verify) for d in devices]
    failures = 0
    emit(f"{'Gerät':<30} {'Status':<14} Hinweis")
    emit("-" * 90)
    with HttpClient(targets=targets, tuning=tuning) as client:
        for device, target in zip(devices, targets, strict=True):
            try:
                key, secret = session.credentials_for(device.id)
            except UnknownDeviceError:
                emit(f"{device.name[:30]:<30} {'NO-CREDS':<14} im Tresor kein API-Key/Secret")
                failures += 1
                continue
            result = check_device(client, target, key, secret)
            status = "OK" if result.is_ok else ("NO-AUTH" if result.reachable else "OFFLINE")
            if not result.is_ok:
                failures += 1
            emit(f"{device.name[:30]:<30} {status:<14} {result.summary}")
    return EXIT_OK if failures == 0 else EXIT_NETWORK_ERROR


def cmd_plan(args: argparse.Namespace, settings: AppSettings) -> int:
    plan_factory = _PLAN_FACTORIES.get(args.plan_action)
    if plan_factory is None:
        emit(
            f"Plan-Aktion '{args.plan_action}' wird in dieser Version nicht unterstützt.",
            err=True,
        )
        return EXIT_GENERAL_ERROR
    session = _unlock_for_command(args, settings)
    if session is None:
        return EXIT_AUTH_ERROR
    store = InventoryStore(session=session)
    devices = store.select(args.target)
    if not devices:
        emit(f"Keine Geräte für Selektor '{args.target}' gefunden.")
        return EXIT_GENERAL_ERROR
    try:
        action_name, subsystem, spec = plan_factory(args)
    except ValueError as exc:
        emit(str(exc), err=True)
        return EXIT_GENERAL_ERROR
    binding = get_binding(subsystem)
    tuning = _tuning_from_session(session)
    targets = [HttpTarget(host=d.host, port=d.port, verify=d.tls_verify) for d in devices]
    audit = AuditLog(path=default_audit_path())
    planner = Planner(
        audit=audit,
        session=session,
        max_workers=session.opened.data.settings.max_workers,
    )
    with HttpClient(targets=targets, tuning=tuning) as client:
        try:
            plan = planner.create_plan(
                action=action_name,
                spec=spec,
                devices=devices,
                adapter=binding.adapter,
                client=client,
            )
        except VaultError as exc:
            emit(str(exc), err=True)
            return EXIT_VAULT_ERROR
    plan_store = PlanStore(base_dir=get_app_data_dir() / "plans")
    plan_path = plan_store.save(plan)
    emit(format_plan_preview(plan))
    emit("")
    emit(format_plan_summary(plan))
    emit("")
    emit(f"Plan gespeichert: {plan_path}")
    emit(f"Apply mit:  opn-cockpit-cli apply {plan.plan_id}")
    return EXIT_OK


def cmd_apply(args: argparse.Namespace, settings: AppSettings) -> int:
    session = _unlock_for_command(args, settings)
    if session is None:
        return EXIT_AUTH_ERROR
    plan_store = PlanStore(base_dir=get_app_data_dir() / "plans")
    try:
        plan = plan_store.load(args.plan)
    except PlanStoreError as exc:
        emit(str(exc), err=True)
        return EXIT_GENERAL_ERROR

    emit(format_plan_preview(plan))
    emit("")
    if not confirm("Diese Aktionen jetzt ausrollen?"):
        emit("Abbruch — nichts wurde ausgeführt.")
        return EXIT_USER_ABORT

    binding = get_binding(plan.subsystem)
    tuning = _tuning_from_session(session)
    devices_in_plan = [a.device for a in plan.actions]
    targets = [HttpTarget(host=d.host, port=d.port, verify=d.tls_verify) for d in devices_in_plan]
    audit = AuditLog(path=default_audit_path())
    executor = Executor(
        session=session,
        audit=audit,
        max_workers=session.opened.data.settings.max_workers,
    )
    with HttpClient(targets=targets, tuning=tuning) as client:
        report = executor.apply(
            plan,
            adapter=binding.adapter,
            controller=binding.controller,
            client=client,
        )
    emit("")
    emit(format_rollout_matrix(
        report,
        devices_by_id={d.id: d.name for d in devices_in_plan},
    ))
    return EXIT_OK if report.failures == 0 else EXIT_NETWORK_ERROR


def cmd_profile(args: argparse.Namespace, settings: AppSettings) -> int:
    """Profile-Handler: list / save-route / save-alias / delete / apply."""
    store = ProfileStore(path=default_profiles_path())
    action = args.profile_action
    if action == "list":
        return _profile_list(store)
    if action == "save-route":
        return _profile_save_route(store, args)
    if action == "save-alias":
        return _profile_save_alias(store, args)
    if action == "delete":
        return _profile_delete(store, args)
    if action == "apply":
        return _profile_apply(store, args, settings)
    emit(f"Unbekannte Profil-Aktion: {action}", err=True)
    return EXIT_GENERAL_ERROR


def _profile_list(store: ProfileStore) -> int:
    try:
        profiles = store.list_profiles()
    except ProfileStoreError as exc:
        emit(str(exc), err=True)
        return EXIT_GENERAL_ERROR
    if not profiles:
        emit("(keine gespeicherten Profile)")
        return EXIT_OK
    emit(f"{'ID':<14} {'Aktion':<14} {'Selektor':<22} Name")
    emit("-" * 90)
    for p in profiles:
        emit(f"{p.id:<14} {p.action:<14} {p.default_selector:<22} {p.name}")
    return EXIT_OK


def _profile_save_route(store: ProfileStore, args: argparse.Namespace) -> int:
    try:
        store.save_new(
            name=args.profile_name,
            action="add_route",
            subsystem="routes",
            default_selector=args.target,
            spec={
                "network": args.network,
                "gateway": args.gateway,
                "descr": args.descr,
                "disabled": bool(args.disabled),
            },
        )
    except ProfileStoreError as exc:
        emit(str(exc), err=True)
        return EXIT_GENERAL_ERROR
    emit(f"Profil '{args.profile_name}' gespeichert.")
    return EXIT_OK


def _profile_save_alias(store: ProfileStore, args: argparse.Namespace) -> int:
    content = [c.strip() for c in args.content.split(",") if c.strip()]
    if not content:
        emit("Mindestens ein Alias-Eintrag erforderlich (--content).", err=True)
        return EXIT_GENERAL_ERROR
    action_name = "append_alias" if args.append else "add_alias"
    try:
        store.save_new(
            name=args.profile_name,
            action=action_name,
            subsystem="firewall_alias",
            default_selector=args.target,
            spec={
                "name": args.alias_name,
                "type": args.alias_type,
                "content": content,
                "descr": args.descr,
                "merge_mode": "append" if args.append else "create",
            },
        )
    except ProfileStoreError as exc:
        emit(str(exc), err=True)
        return EXIT_GENERAL_ERROR
    emit(f"Profil '{args.profile_name}' gespeichert.")
    return EXIT_OK


def _profile_delete(store: ProfileStore, args: argparse.Namespace) -> int:
    try:
        deleted = store.delete(args.profile_id)
    except ProfileStoreError as exc:
        emit(str(exc), err=True)
        return EXIT_GENERAL_ERROR
    if not deleted:
        emit(f"Profil-ID '{args.profile_id}' nicht gefunden.", err=True)
        return EXIT_GENERAL_ERROR
    emit("Profil gelöscht.")
    return EXIT_OK


def _profile_apply(
    store: ProfileStore,
    args: argparse.Namespace,
    settings: AppSettings,
) -> int:
    """Lädt ein Profil und delegiert an cmd_plan/cmd_apply-ähnliche Logik."""
    try:
        profiles = store.list_profiles()
    except ProfileStoreError as exc:
        emit(str(exc), err=True)
        return EXIT_GENERAL_ERROR
    identifier = args.profile_id_or_name
    profile = next(
        (p for p in profiles if identifier in (p.id, p.name)),
        None,
    )
    if profile is None:
        emit(f"Profil '{identifier}' nicht gefunden.", err=True)
        return EXIT_GENERAL_ERROR

    selector = args.target or profile.default_selector
    session = _unlock_for_command(args, settings)
    if session is None:
        return EXIT_AUTH_ERROR
    store_inv = InventoryStore(session=session)
    devices = store_inv.select(selector)
    if not devices:
        emit(f"Selektor '{selector}' liefert keine Geräte.")
        return EXIT_GENERAL_ERROR

    binding = get_binding(profile.subsystem)
    spec = binding.adapter.spec_from_dict(profile.spec)
    tuning = _tuning_from_session(session)
    targets = [HttpTarget(host=d.host, port=d.port, verify=d.tls_verify) for d in devices]
    audit = AuditLog(path=default_audit_path())

    planner = Planner(
        audit=audit,
        session=session,
        max_workers=session.opened.data.settings.max_workers,
    )
    with HttpClient(targets=targets, tuning=tuning) as client:
        plan = planner.create_plan(
            action=profile.action, spec=spec,
            devices=devices, adapter=binding.adapter, client=client,
        )
        emit(format_plan_preview(plan))
        emit("")
        if not confirm("Diese Aktionen jetzt ausrollen?"):
            emit("Abbruch — nichts wurde ausgeführt.")
            return EXIT_USER_ABORT
        executor = Executor(
            session=session, audit=audit,
            max_workers=session.opened.data.settings.max_workers,
        )
        report = executor.apply(
            plan, adapter=binding.adapter, controller=binding.controller, client=client,
        )
    emit("")
    emit(format_rollout_matrix(
        report,
        devices_by_id={d.id: d.name for d in devices},
    ))
    return EXIT_OK if report.failures == 0 else EXIT_NETWORK_ERROR


def cmd_audit(args: argparse.Namespace) -> int:
    audit = AuditLog(path=default_audit_path())
    try:
        event = AuditEventKind(args.event) if args.event else None
    except ValueError:
        emit(f"Unbekannter event-Wert: {args.event}", err=True)
        return EXIT_GENERAL_ERROR
    records = audit.filter(
        event=event,
        action=args.action,
        target_device_id=args.target_device_id,
        actor=args.actor,
        since_iso=args.since_iso,
        until_iso=args.until_iso,
    )
    records = records[-args.limit :] if args.limit > 0 else records
    if not records:
        emit("(keine passenden Einträge)")
        return EXIT_OK
    for rec in records:
        action = rec.action or "-"
        emit(
            f"{rec.timestamp_utc}  {rec.actor:<12}  {rec.event:<22}  {action:<12}  {rec.summary}"
        )
    return EXIT_OK


# ===========================================================================
# Helfer
# ===========================================================================


def _resolve_vault(args: argparse.Namespace, settings: AppSettings) -> Path:
    return resolve_vault_path(getattr(args, "vault", None), settings.default_vault)


def _unlock_for_command(args: argparse.Namespace, settings: AppSettings) -> Session | None:
    """Öffnet den Tresor interaktiv und liefert eine entsperrte Session zurück.

    ``None`` signalisiert Abbruch (falsches Passwort, fehlender Pfad etc.).
    Der Aufrufer übersetzt das in einen Exit-Code.
    """
    try:
        path = _resolve_vault(args, settings)
    except FileNotFoundError as exc:
        emit(str(exc), err=True)
        return None
    if not path.exists():
        emit(f"Tresor-Datei nicht gefunden: {path}", err=True)
        emit("Mit 'create-vault PATH' kannst du einen neuen anlegen.", err=True)
        return None
    pw = prompt_password(f"Master-Passwort für {path}")
    try:
        opened = open_vault(path, pw)
    except InvalidPasswordError:
        _audit_login_failed(path, args.command)
        emit("Master-Passwort falsch.", err=True)
        return None
    except VaultError as exc:
        emit(str(exc), err=True)
        return None
    session = Session()
    session.unlock(opened, path)
    audit = AuditLog(path=default_audit_path())
    audit.append(
        AuditEventKind.VAULT_OPENED,
        vault_path=str(path),
        summary=f"Tresor entsperrt: {path}",
    )
    settings.remember_vault(path)
    settings.save()
    return session


def _audit_login_failed(vault_path: Path, command: str) -> None:
    audit = AuditLog(path=default_audit_path())
    audit.append(
        AuditEventKind.LOGIN_FAILED,
        vault_path=str(vault_path),
        action=command,
        summary=f"Login fehlgeschlagen für Tresor {vault_path} (Befehl: {command}).",
    )


def _tuning_from_session(session: Session) -> HttpTuning:
    settings = session.opened.data.settings
    return HttpTuning(
        connect_timeout_s=settings.connect_timeout_s,
        read_timeout_s=settings.read_timeout_s,
        reconfigure_timeout_s=settings.reconfigure_timeout_s,
        retry_count=settings.retry_count,
    )


if __name__ == "__main__":
    raise SystemExit(main())
