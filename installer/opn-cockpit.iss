; Inno Setup Skript fuer OPN-Cockpit (Embedded-Python-Variante, v6-Pass 2).
;
; Voraussetzung: Inno Setup 6+ (https://jrsoftware.org/isinfo.php).
;
; Build-Reihenfolge:
;   1. installer\bundle-python.ps1   (legt installer\bundle\python\ an)
;   2. ISCC installer\opn-cockpit.iss
;
; Ergebnis:
;   installer\out\OPN-Cockpit-Setup-<Version>.exe
;
; Was der Installer macht:
;   - Kopiert das Bundle (Embedded-Python + alle Dependencies) nach
;     %ProgramFiles%\OPN-Cockpit\python\
;   - Kopiert Source-Tree, Scripts, Docs nach %ProgramFiles%\OPN-Cockpit\
;   - Single-User-Mode: Desktop-Verknuepfung + start.bat
;   - Service-Mode: Registriert NSSM-Dienst, auto-startet
;   - Bei Bedarf: aktualisiert Daten in %APPDATA% bzw. %ProgramData% NICHT
;     (Migrations-Framework laeuft beim ersten Boot, siehe v6-Pass 1)
;
; Aus dem Source-Tree kommen mit:
;   src\, scripts\, start.bat, README.md, docs\, CHANGELOG.md
;   bundle\python\  (vom Build-Skript erzeugt, ~100 MB)
;   bundle\nssm.exe (nur Service-Mode, public domain)
;
; Nicht mit:
;   .venv\, .git\, tests\, __pycache__\, .ruff_cache\, .mypy_cache\

#define MyAppName      "OPN-Cockpit"
#define MyAppVersion   "0.6.0"
#define MyAppPublisher "Ludwig Systems"
#define MyAppURL       "https://github.com/ludwig-systems/opn-cockpit"
#define MyAppExeName   "start.bat"

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
OutputBaseFilename=OPN-Cockpit-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\start.bat

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Types]
Name: "single";  Description: "Single-User (lokaler Desktop-Start)"
Name: "service"; Description: "Multi-User-Server (Windows-Dienst, Autostart)"

[Components]
Name: "single";  Description: "Desktop-Verknuepfung + manueller Start ueber start.bat"; \
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
; vor dem ISCC-Lauf gefuellt.
Source: "bundle\python\*"; DestDir: "{app}\python"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

; Source-Tree und Helfer fuer Updates/Debugging — der Server selbst startet
; aus dem importierten Wheel im Bundle, nicht aus src\. src\ liegt nur als
; Referenz mit (Developers koennen damit auf der Installation experimentieren).
Source: "..\src\*"; DestDir: "{app}\src"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\start.bat";              DestDir: "{app}"; Flags: ignoreversion
Source: "..\pyproject.toml";         DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md";              DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md";           DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\*";                 DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs

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
  WorkingDir: "{app}"; Comment: "OPN-Cockpit starten"; Components: single
Name: "{group}\Quickstart oeffnen"; Filename: "{app}\docs\QUICKSTART.md"
Name: "{group}\{#MyAppName} deinstallieren"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
  WorkingDir: "{app}"; Tasks: desktopicon; Components: single
; Service-Mode: kein Desktop-Shortcut, dafuer Browser-Verknuepfung zum lokalen Port.
Name: "{group}\{#MyAppName} (Web-UI oeffnen)"; Filename: "http://localhost:9876"; Components: service

[Run]
; Service-Mode: Dienst registrieren + starten.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\install-service.ps1"""; \
  WorkingDir: "{app}"; \
  StatusMsg: "Windows-Dienst wird registriert..."; \
  Flags: runhidden; Components: service

; Single-Mode: optional jetzt starten.
Filename: "{app}\{#MyAppExeName}"; \
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
Type: filesandordners; Name: "{app}\python"
Type: filesandordners; Name: "{app}\.venv"
