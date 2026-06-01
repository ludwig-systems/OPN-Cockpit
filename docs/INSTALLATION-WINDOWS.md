# Installation unter Windows

Diese Seite beschreibt den Endnutzer-Pfad: vom Download bis zum ersten
Login. Wenn du das Tool selbst bauen willst, siehe stattdessen
[installer/README.md](../installer/README.md).

## Download

Den aktuellen Release findest du unter
[Releases](https://github.com/ludwig-systems/opn-cockpit/releases/latest).
Lade `Install-OPN-Cockpit-<Version>.exe` herunter (~100 MB).

Die Datei enthält das komplette Tool inklusive Python-Interpreter und
allen Abhängigkeiten — es muss kein System-Python vorinstalliert sein.

## SmartScreen-Warnung

Beim ersten Doppelklick auf die Setup-EXE meldet Windows:

> **Der Computer wurde durch Windows geschützt**
> Microsoft Defender SmartScreen hat den Start einer unbekannten App
> verhindert. Die Ausführung dieser App stellt u. U. ein Risiko für
> den PC dar.

Das liegt daran, dass das Installer-Binary noch nicht
**code-signiert** ist (Authenticode-Zertifikat ist eine kostenpflichtige
Anschaffung und für ein frühes Side-Projekt noch nicht etabliert —
siehe [Roadmap](../installer/README.md#geplante-erweiterungen)).

So bekommst du den Installer trotzdem durch:

1. Im Warnfenster auf **„Weitere Informationen"** klicken.
2. Es erscheint die Zeile **„App: Install-OPN-Cockpit-<Version>.exe"**
   sowie **„Herausgeber: Unbekannter Herausgeber"**.
3. Auf **„Trotzdem ausführen"** klicken.

Das musst du nur einmal pro Download tun.

## Integrität prüfen (optional, empfohlen)

Jeder Release wird mit einer SHA256-Pruefsumme verteilt
(`Install-OPN-Cockpit-<Version>.exe.sha256`). Damit kannst du verifizieren,
dass die heruntergeladene Datei tatsächlich vom offiziellen Build kommt
und nicht beim Download manipuliert wurde:

```powershell
# Im Download-Ordner:
Get-FileHash Install-OPN-Cockpit-0.6.0.exe -Algorithm SHA256
# Den Hash mit der Zeile in der .sha256-Datei vergleichen
Get-Content Install-OPN-Cockpit-0.6.0.exe.sha256
```

Stimmen die Hashes überein, ist die Datei intakt.

## Mode-Wahl im Setup-Wizard

Der Setup-Wizard fragt nach dem Installationsmodus:

| Modus | Wann sinnvoll |
|---|---|
| **Single-User (lokaler Desktop-Start)** | Du bist der einzige Admin. Aufruf über Desktop-Verknüpfung. Daten in `%APPDATA%\OPN-Cockpit\`. |
| **Multi-User-Server (Windows-Dienst, Autostart)** | Mehrere Admins, Tool soll bei Systemstart hochkommen. Daten in `%ProgramData%\OPN-Cockpit\`. Erreichbar im Netzwerk. |

Für den Multi-User-Server-Modus siehe den ausführlichen Bootstrap-Flow
in [SERVICE-MODE.md](SERVICE-MODE.md).

## Erststart (Single-User)

1. Desktop-Verknüpfung **OPN-Cockpit** doppelklicken.
2. Browser öffnet automatisch `http://localhost:9876`.
3. **„Neuen Tresor anlegen…"** klicken.
4. Speicherort und Master-Passwort (min. 12 Zeichen) festlegen.
5. Tresor wird angelegt, du landest auf der leeren Inventar-Ansicht.

Danach kannst du über die Sidebar dein erstes Gerät hinzufügen.

## Updates installieren

Wenn ein neuer Release verfügbar ist:

1. Aktuelle Setup-EXE von [Releases](https://github.com/ludwig-systems/opn-cockpit/releases/latest)
   herunterladen.
2. Doppelklick — der Installer erkennt die vorhandene Installation und
   bietet ein Upgrade an.
3. Beim ersten Start nach dem Update läuft das Migrations-Framework:
   - Falls Schema-Änderungen vorgesehen sind, wird vorher automatisch ein
     **Pre-Update-Backup** unter `<AppData>\backups\<Timestamp>-pre-<Version>\`
     angelegt.
   - Nach erfolgreichem Migrationslauf startet der Server normal.

**Deine Daten (Tresor, Audit, Settings) bleiben bei jedem Update
unangetastet.** Der Installer ersetzt nur den Code-Pfad unter
`%ProgramFiles%\OPN-Cockpit\`.

## Deinstallation

- Systemsteuerung → Programme → OPN-Cockpit → Deinstallieren.
- Der Installations-Ordner `%ProgramFiles%\OPN-Cockpit\` wird entfernt.
- Im Service-Mode entfernt der Uninstaller zusätzlich den Windows-Dienst.

**Was bleibt stehen** (bewusst, damit du nichts versehentlich verlierst):

- `%APPDATA%\OPN-Cockpit\` bzw. `%ProgramData%\OPN-Cockpit\` mit Tresor,
  Audit-Log, User-DB, Backups.

Wenn du das auch loswerden willst, manuell löschen.

## Update-Banner deaktivieren

Das Frontend prüft beim Inventar-Laden anonym gegen
GitHub-Releases-API, ob eine neuere Version verfügbar ist. Im
Standard-Setup ist das aktiv (1× pro 24 h pro Server). Wenn dein
System keinen Internetzugang hat oder du den Check ausschalten willst:

**Single-User**: einmalig vor dem Start setzen (oder via Systemsteuerung
→ Erweiterte Systemeinstellungen → Umgebungsvariablen):

```powershell
[Environment]::SetEnvironmentVariable("OPNCOCKPIT_UPDATE_CHECK_ENABLED", "0", "User")
```

**Multi-User-Server**: in NSSM ergänzen:

```powershell
& "$env:ProgramFiles\OPN-Cockpit\bundle\nssm.exe" set OPN-Cockpit AppEnvironmentExtra `
  "OPNCOCKPIT_UPDATE_CHECK_ENABLED=0"
Restart-Service -Name OPN-Cockpit
```

Danach liefert `/api/updates/check` `status: "disabled"` und der Banner
erscheint nicht mehr.

## Hilfe bei Problemen

- **Server kommt nicht hoch**: `%APPDATA%\OPN-Cockpit\` löschen und
  Service neu starten — der Setup-Wizard kommt dann erneut. (Achtung:
  vorher Tresor sichern!)
- **Logs Service-Mode**: `%ProgramData%\OPN-Cockpit\logs\stderr.log`
- **Logs Single-Mode**: Konsolen-Fenster des Launchers zeigt den Trace.
- **Issues melden**:
  [github.com/ludwig-systems/opn-cockpit/issues](https://github.com/ludwig-systems/opn-cockpit/issues)
