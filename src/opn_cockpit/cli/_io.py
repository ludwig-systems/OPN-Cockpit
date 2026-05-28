"""CLI-I/O-Helfer: Prompts, Bestätigung, Pfad-Auflösung.

Bewusst klein gehalten — nur das, was die ``main.py`` mehrfach braucht.
Keine UI-Bibliothek (rich/click), damit das Tool ohne Extra-Dependencies
auf einer minimalen PAW-Installation läuft.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable
from pathlib import Path


def prompt_password(label: str, *, getpass_fn: Callable[[str], str] = getpass.getpass) -> str:
    """Fragt ein Passwort ab. Dünner Wrapper, der für Tests injizierbar ist."""
    return getpass_fn(f"{label}: ")


def prompt_password_with_confirmation(
    label: str,
    *,
    getpass_fn: Callable[[str], str] = getpass.getpass,
) -> str:
    """Fragt ein Passwort zweimal ab und wirft ``ValueError`` bei Abweichung."""
    pw1 = getpass_fn(f"{label}: ")
    pw2 = getpass_fn(f"{label} (Wiederholung): ")
    if pw1 != pw2:
        raise ValueError("Eingaben stimmen nicht überein.")
    return pw1


def confirm(
    prompt: str,
    *,
    keyword: str = "ja",
    input_fn: Callable[[str], str] = input,
) -> bool:
    """Erzwingt ein explizites Wort als Bestätigung (R-PRE-2).

    Default: Antwort muss ``ja`` lauten — eine versehentliche ``y``/Enter-
    Eingabe genügt nicht. Lokalisiert auf Deutsch.
    """
    raw = input_fn(f"{prompt} Bitte '{keyword}' eintippen, alles andere bricht ab: ")
    return raw.strip().lower() == keyword


def emit(msg: str, *, err: bool = False) -> None:
    """Schreibt eine Zeile auf stdout (Default) oder stderr."""
    stream = sys.stderr if err else sys.stdout
    stream.write(msg + "\n")
    stream.flush()


def resolve_vault_path(arg: str | None, default: str | None) -> Path:
    """Bestimmt den Tresor-Pfad aus CLI-Argument bzw. App-Settings.

    Wirft ``FileNotFoundError`` wenn weder ``arg`` noch ``default`` gesetzt
    ist — der Aufrufer übersetzt das in eine sprechende CLI-Meldung.
    """
    if arg:
        return Path(arg).expanduser()
    if default:
        return Path(default).expanduser()
    raise FileNotFoundError(
        "Kein Tresor-Pfad angegeben (--vault) und kein Default in den App-Settings."
    )
