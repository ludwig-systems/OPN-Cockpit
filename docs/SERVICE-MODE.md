# Windows-Service-Mode (v3.2)

Statt OPN-Cockpit jedes Mal manuell via `start.bat` zu starten, kannst
du es als Windows-Dienst registrieren. Der Server läuft dann ab dem
Systemstart, kommt nach jedem Reboot automatisch hoch und ist über
Netzwerk erreichbar (Standard: `http://<host>:9876`).

## Installation

### Variante A — über den Installer (empfohlen)

Wenn du den Inno-Setup-Installer baust und „Multi-User-Server" wählst,
richtet der Installer den Dienst selbst ein. Voraussetzung:
`installer\bundle\nssm.exe` muss vor dem Build vorhanden sein.

### Variante B — manuell auf einem bestehenden Single-Mode-Setup

1. NSSM von https://nssm.cc/ herunterladen.
2. `nssm.exe` nach `<Installation>\bundle\nssm.exe` legen.
3. Admin-PowerShell öffnen, ins Installations-Verzeichnis wechseln.
4. Skript ausführen:
   ```powershell
   .\scripts\install-service.ps1
   ```

Das Skript:
- Stoppt einen evtl. existierenden Dienst gleichen Namens
- Registriert `OPN-Cockpit` als NSSM-Service
- Setzt `OPNCOCKPIT_HOST=0.0.0.0`, `OPNCOCKPIT_PORT=9876`,
  `OPNCOCKPIT_NO_BROWSER=1`
- Routet stdout/stderr nach `%ProgramData%\OPN-Cockpit\logs\`
- Startet den Dienst

## Multi-User-Modus aktivieren

Nach der Service-Registrierung läuft der Server zunächst im
Single-User-Mode. Multi-User aktivieren:

```powershell
# Admin-PowerShell, im Installations-Verzeichnis
.\bundle\nssm.exe edit OPN-Cockpit
```

Im „Environment"-Tab folgende Variablen ergänzen:

```
OPNCOCKPIT_AUTH_BACKEND=user-db
OPNCOCKPIT_DEPLOYMENT_MODE=multi-server
OPNCOCKPIT_VAULT_PATH=C:\ProgramData\OPN-Cockpit\firewalls.opnvault
```

Den Vault musst du **vorher** im Single-Mode angelegt haben (oder den
Pfad auf einen bestehenden zeigen). Dann:

```powershell
Restart-Service -Name OPN-Cockpit
```

Beim ersten Aufruf auf `http://localhost:9876` erscheint der
Setup-Wizard. Den **Bootstrap-Token** liest du aus dem Log:

```powershell
Get-Content "$env:ProgramData\OPN-Cockpit\logs\stderr.log" -Tail 20
```

Das Setup verlangt den Token plus deine Admin-Credentials. Nach
erfolgreichem Bootstrap rotiert der Token einmalig für den Vault-
Unlock-Schritt (im Log erscheint ein zweiter Eintrag).

## Service verwalten

```powershell
Get-Service -Name OPN-Cockpit                    # Status
Start-Service -Name OPN-Cockpit                  # starten
Stop-Service -Name OPN-Cockpit                   # stoppen
Restart-Service -Name OPN-Cockpit                # neustarten
```

Logs:
- stdout: `%ProgramData%\OPN-Cockpit\logs\stdout.log`
- stderr: `%ProgramData%\OPN-Cockpit\logs\stderr.log` (enthält
  Bootstrap-Token bei Status-Wechseln)

NSSM rotiert die Logs automatisch bei 5 MiB.

## Dienst entfernen

```powershell
.\scripts\uninstall-service.ps1
```

oder über den Inno-Uninstaller (führt das gleiche Skript aus).

Vault-, Audit- und User-DB-Dateien bleiben erhalten — die liegen
außerhalb der Installation.

## Reverse-Proxy davor

Sobald der Server im Office-LAN erreichbar ist, gehört ein
TLS-terminierender Reverse-Proxy davor (nginx / Caddy / IIS) mit
Let's-Encrypt-Zertifikat oder internem CA-Zertifikat. Siehe
[DOCKER.md](DOCKER.md) für ein nginx-Beispiel — analog für IIS.

Im Service-Mode bindet OPN-Cockpit per Default auf `0.0.0.0:9876`.
Wenn du den Server hinter einem Reverse-Proxy laufen lassen willst,
setze in NSSM zusätzlich `OPNCOCKPIT_HOST=127.0.0.1` — dann ist er
nur lokal erreichbar und der Proxy macht die Außenverbindung.
