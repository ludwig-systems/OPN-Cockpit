; Inno Setup Skript fuer OPN-Cockpit (Embedded-Python-Variante, v6-Pass 2).
;
; Voraussetzung: Inno Setup 6+ (https://jrsoftware.org/isinfo.php).
;
; Build-Reihenfolge:
;   1. installer\bundle-python.ps1   (legt installer\bundle\python\ an)
;   2. ISCC installer\opn-cockpit.iss
;
; Ergebnis:
;   installer\out\Install-OPN-Cockpit-<Version>.exe
;
; Was der Installer macht:
;   - Kopiert das Bundle (Embedded-Python + alle Dependencies) nach
;     %ProgramFiles%\OPN-Cockpit\python\
;   - Legt eine minimale README.md im Programm-Ordner ab (kurzer Kontext)
;   - Single-User-Mode: Desktop-Verknuepfung auf opn-cockpit.exe
;     (kein start.bat — die EXE legt pip aus dem entry_point an)
;   - Service-Mode: Registriert NSSM-Dienst, auto-startet
;   - Bei Bedarf: aktualisiert Daten in %APPDATA% bzw. %ProgramData% NICHT
;     (Migrations-Framework laeuft beim ersten Boot, siehe v6-Pass 1)
;
; Aus dem Source-Tree kommen mit:
;   bundle\python\  (vom Build-Skript erzeugt, ~100 MB — enthaelt auch
;                    Scripts\opn-cockpit.exe als Launcher)
;   README.md       (einziges Doku-Asset)
;   bundle\nssm.exe (nur Service-Mode, public domain)
;
; Nicht mit:
;   docs\           (Maintainer-Material — Roadmap, Releasing, TestPlan etc.
;                    liegt auf GitHub, Endnutzer kommt ueber About-Modal +
;                    Startmenue-Link "Online-Hilfe" dran)
;   CHANGELOG.md    (Versionshistorie auf GitHub, Version steht im UI)
;   .venv\, .git\, tests\, src\, start.bat, __pycache__\, .ruff_cache\

#define MyAppName       "OPN-Cockpit"
#define MyAppVersion    "0.6.0"
#define MyAppPublisher  "Ludwig Systems"
#define MyAppURL        "https://github.com/ludwig-systems/opn-cockpit"
; opn-cockpit.exe ist eine Kopie der Embedded-Python python.exe in derselben
; Datei-Lage -- bundle-python.ps1 legt das an. Image-Name im Task-Manager =
; opn-cockpit.exe, kein pip-Launcher mit Build-Zeit-Pfad-Shebang noetig.
#define MyAppExeName    "python\opn-cockpit.exe"
#define MyAppExeArgs    "-m opn_cockpit"

[Setup]
AppId={{B8F1A7C2-9D6E-4F3B-A1C0-OPNCOCKPITV20}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=.\out
OutputBaseFilename=Install-OPN-Cockpit-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Types]
Name: "single";  Description: "Single-User (lokaler Desktop-Start)"
Name: "service"; Description: "Multi-User-Server (Windows-Dienst, Autostart)"

[Components]
Name: "single";  Description: "Desktop-Verknuepfung, manueller Start per Doppelklick"; \
  Types: single;  Flags: exclusive
Name: "service"; Description: "Windows-Dienst registrieren (Autostart, NSSM-basiert)"; \
  Types: service; Flags: exclusive

[Tasks]
Name: "desktopicon"; Description: "Desktop-Verknuepfung anlegen"; \
  GroupDescription: "Zusaetzliche Symbole:"; Components: single

[Files]
; Embedded-Python-Bundle — der gesamte selbsttragende Interpreter inklusive
; aller Dependencies (httpx, fastapi, uvicorn, cryptography, etc.) plus die
; installierte opn-cockpit-Distribution. Wird von installer\bundle-python.ps1
; vor dem ISCC-Lauf gefuellt. Enthaelt auch Scripts\opn-cockpit.exe als
; konsolen-basierten Launcher (Console-Subsystem, entry_point aus pyproject).
Source: "bundle\python\*"; DestDir: "{app}\python"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

; Einziges Doku-Asset: README — kurzer Was-ist-das-Kontext im Install-Ordner.
; Alles weitere (Roadmap, Releasing, TestPlan, Security-Audit, ...) bleibt
; auf GitHub, der Endnutzer kommt darueber den Start-Menue-Link "Online-Hilfe"
; oder das About-Modal hin.
Source: "..\README.md";              DestDir: "{app}"; Flags: ignoreversion

; Service-Mode Helfer
Source: "..\scripts\install-service.ps1";   DestDir: "{app}\scripts"; Flags: ignoreversion; Components: service
Source: "..\scripts\uninstall-service.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion; Components: service

; NSSM-Bundle - Build-Schritt muss `installer\bundle\nssm.exe` vorlegen
; (Download von nssm.cc, public domain). Optional: existiert nur wenn
; Service-Komponente gewaehlt.
Source: "bundle\nssm.exe";           DestDir: "{app}\bundle"; \
  Flags: ignoreversion skipifsourcedoesntexist; Components: service

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
  Parameters: "{#MyAppExeArgs}"; WorkingDir: "{app}\python"; \
  Comment: "OPN-Cockpit starten"; Components: single
Name: "{group}\Online-Hilfe"; Filename: "{#MyAppURL}"
Name: "{group}\{#MyAppName} deinstallieren"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
  Parameters: "{#MyAppExeArgs}"; WorkingDir: "{app}\python"; \
  Tasks: desktopicon; Components: single
; Service-Mode: kein Desktop-Shortcut, dafuer Browser-Verknuepfung zum lokalen Port.
Name: "{group}\{#MyAppName} (Web-UI oeffnen)"; Filename: "http://localhost:9876"; Components: service

[Run]
; Service-Mode: Dienst registrieren + starten.
; Bewusst KEIN runhidden — wenn das Script (Group-Policy / Antivirus / NSSM
; fehlt) scheitert, soll der User das im Konsolen-Fenster sehen statt sich
; spaeter zu wundern wo der Dienst ist. waituntilterminated blockiert den
; Installer-Fortschritt bis das Setup wirklich durch ist.
; Das Script zeigt am Ende den Bootstrap-Token in einer MessageBox.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\install-service.ps1"""; \
  WorkingDir: "{app}"; \
  StatusMsg: "Windows-Dienst wird registriert..."; \
  Flags: waituntilterminated; Components: service

; Single-Mode: optional jetzt starten.
Filename: "{app}\{#MyAppExeName}"; \
  Parameters: "{#MyAppExeArgs}"; \
  WorkingDir: "{app}\python"; \
  Description: "OPN-Cockpit jetzt starten"; \
  Flags: nowait postinstall skipifsilent; Components: single

; Service-Mode: Browser zum lokalen Server oeffnen.
Filename: "http://localhost:9876"; \
  Description: "OPN-Cockpit im Browser oeffnen"; \
  Flags: postinstall skipifsilent shellexec; Components: service

[UninstallRun]
; Service-Mode: bei Deinstall den Dienst sauber entfernen.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\uninstall-service.ps1"""; \
  RunOnceId: "UninstallService"; Flags: runhidden; Components: service

[UninstallDelete]
; Embedded-Python-Bundle wird mit deinstalliert. AppData/ProgramData bleibt
; bewusst stehen (Tresor, Audit, Settings).
Type: filesandordirs; Name: "{app}\python"
