# Third-Party Notices

OPN-Cockpit ist unter der Apache License 2.0 lizenziert (siehe `LICENSE`).
Dieses Dokument listet die Drittanbieter-Software auf, die OPN-Cockpit zur
Laufzeit oder im Distributions-Bundle einbindet.

Alle aufgefuehrten Lizenzen sind mit Apache 2.0 kompatibel.

---

## Python Runtime-Abhaengigkeiten

(aus `pyproject.toml`)

| Paket                | Lizenz                | Zweck                                  |
| -------------------- | --------------------- | -------------------------------------- |
| **fastapi**          | MIT                   | REST-Framework                         |
| **uvicorn**          | BSD-3-Clause          | ASGI-Server                            |
| **starlette**        | BSD-3-Clause          | ASGI-Foundation (von FastAPI gezogen)  |
| **pydantic**         | MIT                   | Schema-Validierung (von FastAPI gezogen) |
| **httpx**            | BSD-3-Clause          | Async/Sync HTTP-Client                 |
| **jinja2**           | BSD-3-Clause          | HTML-Templates                         |
| **python-multipart** | Apache-2.0            | FastAPI Form-Parsing                   |
| **cryptography**     | Apache-2.0 / BSD      | AES-GCM Vault-Verschluesselung         |
| **argon2-cffi**      | MIT                   | KDF fuer Master-Passwort               |
| **fpdf2**            | LGPL-3.0+             | PDF-Audit-Report                       |
| **paramiko**         | LGPL-2.1              | SSH-Client fuer Safety-Net-Rollback    |
| **pillow**           | MIT-CMU               | Indirekte Abhaengigkeit von fpdf2      |
| **fonttools**        | MIT                   | Indirekte Abhaengigkeit von fpdf2      |
| **bcrypt**           | Apache-2.0            | Indirekte Abhaengigkeit von paramiko   |
| **pynacl**           | Apache-2.0            | Indirekte Abhaengigkeit von paramiko   |

**LGPL-Hinweis**: `fpdf2` und `paramiko` stehen unter LGPL. OPN-Cockpit
verknuepft sie dynamisch zur Laufzeit (Python-Import); damit gilt die
LGPL-Linking-Klausel - kein Copyleft-Effekt auf den OPN-Cockpit-Source.
Wer die LGPL-Komponenten ersetzen will, kann das ohne Anpassung am
OPN-Cockpit-Code tun (sie sind in `pyproject.toml` als normale Deps
gelistet).

---

## Bundle-Komponenten (Windows-Installer + Linux-Pakete)

| Komponente            | Lizenz                                  | Zweck                                 |
| --------------------- | --------------------------------------- | ------------------------------------- |
| **Python Embedded**   | PSF License Version 2                   | Runtime im Windows-Single-User-Bundle |
| **NSSM**              | Public Domain (Wrapper) / per Library   | Windows-Service-Wrapper (Multi-User)  |
| **Inno Setup**        | Modified BSD                            | Installer-Builder                     |

Hinweis zu NSSM: Der NSSM-Wrapper selbst ist Public Domain, bindet aber
Bibliotheken mit eigenen Lizenzen ein. Siehe `nssm.exe`-Aufruf `nssm
license` fuer die vollstaendige Auflistung.

---

## OPNsense

OPN-Cockpit kommuniziert mit OPNsense ueber die offizielle REST-API.
OPNsense selbst ist BSD-2-Clause-lizenziert (Copyright 2014-2026 Deciso
B.V.) und wird **nicht** mit OPN-Cockpit gebuendelt - es laeuft auf
separater Hardware/VM.

---

## Aktualisierungspolitik

Bei Aenderungen an `pyproject.toml` oder neuen Bundle-Komponenten ist
dieser Hinweis zu aktualisieren. Im CI-Workflow `release.yml` koennte
spaeter eine automatische Abgleich-Pruefung gegen die `pip`-Ausgabe
ergaenzt werden (Tracking-Issue: tbd).
