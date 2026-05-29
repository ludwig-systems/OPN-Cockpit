"""GUI-Entry-Point: ``QApplication``-Setup, sys.excepthook und Login-Routing.

Spec-Referenzen:

* **R-SEC-5/R-LOG-3** — ``sys.excepthook`` schickt Tracebacks und den
  String-Wert der Exception vor jedem Output durch ``security.masking.mask_dict``,
  damit ein Crash nie Klartext-Secrets in ein Dialogfenster oder die Konsole
  leakt.
* **R-SEC-6** — Inaktivitätssperre wird im :class:`MainWindow` per QTimer
  verwaltet (siehe ``main_window.py``).
* **R-SEC-1** — App-Start öffnet zuerst ein Login-Fenster mit Tresor-Pfad-
  Auswahl und Master-Passwort.
"""

from __future__ import annotations

import contextlib
import sys
import traceback
import types
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from opn_cockpit.audit.log import AuditEventKind, AuditLog, default_audit_path
from opn_cockpit.config import AppSettings
from opn_cockpit.gui.login_dialog import LoginDialog, LoginResult
from opn_cockpit.gui.main_window import MainWindow
from opn_cockpit.security.masking import mask_dict
from opn_cockpit.security.session import Session
from opn_cockpit.vault.discovery import discover_vaults
from opn_cockpit.vault.errors import VaultError
from opn_cockpit.vault.store import create_vault, open_vault

# ---------------------------------------------------------------------------
# Masking-Excepthook
# ---------------------------------------------------------------------------


_ORIGINAL_EXCEPTHOOK = sys.excepthook


def install_masking_excepthook() -> None:
    """Registriert einen ``sys.excepthook``, der Tracebacks vorab maskiert.

    Wichtig: ``traceback.format_exception`` zeigt nur die Klassennamen +
    ``str(exc)``, **keine** lokalen Variablen. Wir maskieren zusätzlich den
    str-Anteil über ``mask_dict``, falls sich da ein Secret eingeschlichen
    hat (z. B. weil jemand eine Exception mit dem rohen Body geworfen hat).
    """

    def hook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: types.TracebackType | None,
    ) -> None:
        raw_lines = traceback.format_exception(exc_type, exc, tb)
        masked_lines = [
            _mask_line(line) for line in raw_lines
        ]
        masked_text = "".join(masked_lines)

        # Stderr fallback (für CLI/Headless-Tests):
        sys.stderr.write(masked_text)
        sys.stderr.flush()

        # Falls eine GUI läuft, zusätzlich ein Dialogfenster (best-effort).
        app = QApplication.instance()
        if app is not None:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("Unerwarteter Fehler")
            box.setText("OPN-Cockpit ist auf einen unerwarteten Fehler gestoßen.")
            box.setDetailedText(masked_text)
            box.exec()

        # Audit-Eintrag des Crashs (ohne Trace, nur Klasse + maskierte Summary)
        try:
            audit = AuditLog(path=default_audit_path())
            audit.append(
                AuditEventKind.LOGIN_FAILED,  # eigener "crash"-Event wäre besser;
                # für v1 reicht das nicht-spezifische Fallback-Event
                summary=f"Crash: {exc_type.__name__} — siehe stderr/Dialog.",
            )
        except Exception:
            pass

    sys.excepthook = hook


def restore_excepthook() -> None:
    sys.excepthook = _ORIGINAL_EXCEPTHOOK


def _mask_line(line: str) -> str:
    """Vorsichtige Heuristik: maskiert ``key=value``-Paare, deren Schlüssel
    nach einem Geheimnis aussieht."""
    # Wir parsen nicht aufwendig — bekannte Secret-Wörter werden zensiert.
    # Tracebacks lokaler Variablen tauchen mit ``-tb`` ohnehin nicht auf,
    # aber Exception-Strings könnten unvorsichtig formatiert sein.
    masked = mask_dict({"line": line}).get("line", line)
    return str(masked)


# ---------------------------------------------------------------------------
# Boot-Sequenz
# ---------------------------------------------------------------------------


def run() -> int:
    """Startet die GUI. Liefert den Exit-Code zurück."""
    install_masking_excepthook()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("OPN-Cockpit")
    app.setOrganizationName("OPN-Cockpit")

    settings = AppSettings.load()

    while True:
        available = discover_vaults(settings)
        login = LoginDialog(available_vaults=available)
        if login.exec() != LoginDialog.DialogCode.Accepted:
            return 0
        result: LoginResult | None = login.result_data
        if result is None:
            return 0

        # Wenn der User in dem Dialog "Neuen Tresor anlegen" geklickt hat,
        # legen wir die Datei jetzt physisch an. Der LoginResult enthaelt
        # bereits Pfad + Passwort des frischen Tresors.
        if login.created_vault is not None:
            try:
                create_vault(result.path, result.password)
            except VaultError as exc:
                QMessageBox.critical(
                    None,
                    "Tresor anlegen fehlgeschlagen",
                    f"Tresor konnte nicht angelegt werden:\n{exc}",
                )
                continue

        try:
            opened = open_vault(result.path, result.password)
        except Exception as exc:
            QMessageBox.critical(
                None,
                "Login fehlgeschlagen",
                f"Tresor konnte nicht entsperrt werden:\n{exc}",
            )
            _audit_login_failed(result.path)
            continue

        session = Session()
        session.unlock(opened, result.path)
        _audit_vault_opened(result.path)

        # AppSettings nachziehen
        settings.remember_vault(result.path)
        if settings.default_vault is None:
            settings.default_vault = str(result.path)
        with contextlib.suppress(OSError):
            settings.save()

        window = MainWindow(session=session, app_settings=settings)
        window.show()

        rc = app.exec()
        # Nach Schließen des Fensters: Tresor sperren und beenden.
        session.lock()
        return rc


def _audit_vault_opened(path: Path) -> None:
    audit = AuditLog(path=default_audit_path())
    audit.append(
        AuditEventKind.VAULT_OPENED,
        vault_path=str(path),
        summary=f"Tresor entsperrt: {path}",
    )


def _audit_login_failed(path: Path) -> None:
    audit = AuditLog(path=default_audit_path())
    audit.append(
        AuditEventKind.LOGIN_FAILED,
        vault_path=str(path),
        summary=f"GUI-Login fehlgeschlagen für {path}",
    )


if __name__ == "__main__":
    raise SystemExit(run())
