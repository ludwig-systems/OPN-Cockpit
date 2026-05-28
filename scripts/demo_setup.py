"""Demo-Setup fuer eine GUI-Tour ohne echte OPNsense.

Erzeugt einen Demo-Tresor unter ``%LOCALAPPDATA%/Temp/opn-cockpit-demo.opnvault``
(oder dem Home-Verzeichnis als Fallback) mit drei Beispiel-Geraeten und
startet anschliessend die GUI.

Die drei Demo-Geraete sind so gewaehlt, dass das TCP-Heartbeat-Puenktchen
sichtbar arbeitet, ohne dass eine echte OPNsense im Spiel sein muss:

* ``demo-localhost`` -> 127.0.0.1:443 (i. d. R. rot, weil lokal kein HTTPS)
* ``demo-unreachable`` -> 10.255.255.255:443 (TCP-Timeout -> rot)
* ``demo-dns-fail`` -> opn-nonexistent.invalid:443 (DNS-Fail -> rot)

Du kannst die Hosts spaeter in der GUI direkt bearbeiten, falls du einen
real erreichbaren Host fuer die gruene Anzeige sehen willst.

Verwendung::

    .\\.venv\\Scripts\\python.exe scripts\\demo_setup.py

Master-Passwort des Demo-Tresors: ``opn-demo-passwort-2026``
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

from opn_cockpit.config import AppSettings
from opn_cockpit.gui.app import run
from opn_cockpit.vault.errors import VaultError
from opn_cockpit.vault.model import VaultData, VaultDevice, VaultSettings
from opn_cockpit.vault.store import create_vault, open_vault

DEMO_PASSWORD = "opn-demo-passwort-2026"


def _demo_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP")
    base_dir = Path(base) if base else Path.home()
    return base_dir / "opn-cockpit-demo.opnvault"


def _build_demo_data() -> VaultData:
    devices = [
        VaultDevice(
            id=VaultDevice.new_id(),
            name="Demo Localhost",
            host="127.0.0.1",
            port=443,
            tls_verify=False,
            tags=["demo", "lokal"],
            api_key="demo-key",
            api_secret="demo-secret-not-real",
            descr="Lokaler Loopback - Heartbeat zeigt rot, wenn kein HTTPS-Server laeuft.",
        ),
        VaultDevice(
            id=VaultDevice.new_id(),
            name="Demo Unreachable",
            host="10.255.255.255",
            port=443,
            tls_verify=False,
            tags=["demo", "branches"],
            api_key="demo-key",
            api_secret="demo-secret-not-real",
            descr="RFC-reservierte IP - TCP-Probe laeuft in Timeout, Heartbeat rot.",
        ),
        VaultDevice(
            id=VaultDevice.new_id(),
            name="Demo DNS-Fail",
            host="opn-nonexistent.invalid",
            port=443,
            tls_verify=True,
            tags=["demo", "branches", "germany"],
            api_key="demo-key",
            api_secret="demo-secret-not-real",
            descr="Nicht aufloesbarer Hostname - DNS-Fail, Heartbeat rot.",
        ),
    ]
    return VaultData(
        devices=devices,
        settings=VaultSettings(inactivity_minutes=30),
    )


def main() -> int:
    path = _demo_path()
    if not path.exists():
        print(f"Lege Demo-Tresor an: {path}")
        create_vault(path, DEMO_PASSWORD, _build_demo_data())
    else:
        print(f"Demo-Tresor existiert bereits: {path}")
        try:
            open_vault(path, DEMO_PASSWORD)
        except VaultError as exc:
            print(
                f"Vorhandene Demo-Datei nicht mit dem Demo-Passwort entsperrbar: {exc}",
                file=sys.stderr,
            )
            print(
                "Loesche die Datei oder benutze ein anderes Passwort, "
                "dann nochmal starten.",
                file=sys.stderr,
            )
            return 1

    print()
    print("=" * 70)
    print("Master-Passwort fuer den Demo-Tresor:")
    print(f"    {DEMO_PASSWORD}")
    print()
    print("Die GUI startet jetzt. Im Login-Dialog ist der Tresor-Pfad")
    print("bereits vorausgefuellt - einfach das Passwort eintippen und auf")
    print("'Entsperren' klicken.")
    print("=" * 70)
    print()

    settings = AppSettings.load()
    settings.remember_vault(path)
    if settings.default_vault is None:
        settings.default_vault = str(path)
    with contextlib.suppress(OSError):
        settings.save()

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
