"""Vault-Discovery: Welche ``.opnvault``-Dateien hat dieser User schon?

v1.2: Beim GUI-Start soll der User nicht selbst durch den Dateibrowser
klicken muessen. Die App scannt das App-Daten-Verzeichnis nach Tresoren
und mischt die Recent-Vaults-Liste aus den App-Settings dazu. So entsteht
eine kuratierte Auswahl, die dem Boot-Dialog uebergeben wird.
"""

from __future__ import annotations

from pathlib import Path

from opn_cockpit.config import AppSettings, get_app_data_dir

VAULT_EXTENSION = ".opnvault"


def discover_vaults(settings: AppSettings | None = None) -> list[Path]:
    """Liefert alle bekannten Tresor-Pfade in stabiler Reihenfolge.

    Reihenfolge:

    1. Default-Tresor aus den Settings (falls existiert) — immer als erstes,
       damit beim 1-Treffer-Fall direkt das richtige Passwort-Eingabe-Feld
       erscheint.
    2. Tresoren im App-Daten-Verzeichnis (``%APPDATA%/OPN-Cockpit/*.opnvault``),
       alphabetisch.
    3. Recent-Vaults aus den App-Settings, die noch auf Platte liegen und
       nicht schon in 1./2. drinstehen.

    Duplikate werden uebersprungen (Pfad-basiert, nach :meth:`Path.resolve`).
    """
    settings = settings or AppSettings.load()
    seen: set[Path] = set()
    out: list[Path] = []

    # 1. Default
    if settings.default_vault:
        default_p = Path(settings.default_vault)
        if default_p.exists() and default_p.suffix == VAULT_EXTENSION:
            resolved = _resolve(default_p)
            if resolved not in seen:
                seen.add(resolved)
                out.append(default_p)

    # 2. App-Daten-Verzeichnis
    app_data = get_app_data_dir()
    if app_data.exists():
        for p in sorted(app_data.glob(f"*{VAULT_EXTENSION}")):
            resolved = _resolve(p)
            if resolved not in seen:
                seen.add(resolved)
                out.append(p)

    # 3. Recents (in Settings-Reihenfolge, neuestes zuerst)
    for raw in settings.recent_vaults:
        p = Path(raw)
        if not p.exists() or p.suffix != VAULT_EXTENSION:
            continue
        resolved = _resolve(p)
        if resolved not in seen:
            seen.add(resolved)
            out.append(p)

    return out


def default_new_vault_path() -> Path:
    """Liefert einen sinnvollen Default-Pfad fuer einen frisch angelegten Tresor."""
    return get_app_data_dir() / f"main{VAULT_EXTENSION}"


def suggested_vault_locations() -> list[tuple[str, Path]]:
    """Liefert eine Liste ``(Label, Verzeichnis)`` fuer den Speicherort-Quick-Pick.

    Browser koennen keinen nativen Save-File-Dialog ausloesen, deshalb
    bekommt der User stattdessen drei klickbare Default-Verzeichnisse
    unter dem Speicherort-Feld. Reihenfolge ist user-orientiert: zuerst
    der typische "Eigene Dokumente"-Speicherort (mit OPN-Cockpit-
    Unterordner), danach der Desktop, am Ende die Anwendungsdaten.

    Es werden **Verzeichnisse** zurueckgegeben, nicht ganze Datei-Pfade —
    der Dateiname kommt aus einem separaten Eingabefeld im Frontend.
    """
    home = Path.home()
    locations: list[tuple[str, Path]] = []

    documents = home / "Documents"
    if documents.exists():
        locations.append(("Eigene Dokumente", documents / "OPN-Cockpit"))

    desktop = home / "Desktop"
    if desktop.exists():
        locations.append(("Desktop", desktop / "OPN-Cockpit"))

    locations.append(("Anwendungsdaten", get_app_data_dir()))
    return locations


def default_vault_basename() -> str:
    """Default-Stem fuer einen neu angelegten Tresor (ohne .opnvault-Endung)."""
    return "main"


def _resolve(path: Path) -> Path:
    """Resolved Pfad fuer Vergleichszwecke; fallback auf den Original-Pfad."""
    try:
        return path.resolve()
    except OSError:
        return path
