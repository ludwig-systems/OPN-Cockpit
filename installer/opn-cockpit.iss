; Inno Setup Skript fuer OPN-Cockpit v2.0
;
; Voraussetzung: Inno Setup 6+ (https://jrsoftware.org/isinfo.php).
; Build: ISCC opn-cockpit.iss   (oder per GUI)
;
; Was der Installer macht:
;   - Kopiert das Repo (ohne .git/.venv/node_modules etc.) nach
;     %ProgramFiles%\OPN-Cockpit\
;   - Legt eine portable Python-3.11-Embedded-Distribution daneben
;     (PythonEmbedDir muss vor dem Build manuell vorbereitet werden)
;   - Installiert die Runtime-Dependencies via embedded pip in
;     %ProgramFiles%\OPN-Cockpit\python\Lib\site-packages
;   - Legt Desktop-Verknuepfung "OPN-Cockpit" an, die start.bat aufruft
;   - Legt Start-Menue-Eintrag an
;   - Speichert Tresor-Dateien NICHT mit (die liegen in %APPDATA%)
;
; Aus dem Source-Tree kommen mit:
;   src\, scripts\, start.bat, README.md, docs\, mockups\, CHANGELOG.md
;
; Nicht mit:
;   .venv\, .git\, tests\, __pycache__\, .ruff_cache\, .mypy_cache\

#define MyAppName      "OPN-Cockpit"
#define MyAppVersion   "0.1.0"
#define MyAppPublisher "OPN-Cockpit Maintainers"
#define MyAppURL       "https://github.com/your-org/opn-cockpit"
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

[Tasks]
Name: "desktopicon"; Description: "Desktop-Verknuepfung anlegen"; \
  GroupDescription: "Zusaetzliche Symbole:"

[Files]
; Tree-Inhalt minus die nicht-relevanten Ordner.
; "..\" weil das Skript in installer\ liegt.
Source: "..\src\*"; DestDir: "{app}\src"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\scripts\setup-venv.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\demo_setup.py";  DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\start.bat";              DestDir: "{app}"; Flags: ignoreversion
Source: "..\pyproject.toml";         DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md";              DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md";           DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\*";                 DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
  WorkingDir: "{app}"; Comment: "OPN-Cockpit starten"
Name: "{group}\Quickstart oeffnen"; Filename: "{app}\docs\QUICKSTART.md"
Name: "{group}\{#MyAppName} deinstallieren"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
  WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; Nach dem Kopieren: venv anlegen + Dependencies installieren.
; Erwartet, dass auf dem Zielsystem Python 3.11+ und uv installiert sind.
; (Embedded-Python-Pfad wird in spaeterer Iteration als Alternativvariante
; ergaenzt — hier zunaechst die einfachere Variante mit System-Python.)
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\setup-venv.ps1"""; \
  WorkingDir: "{app}"; \
  StatusMsg: "Python-Umgebung wird eingerichtet (kann eine Minute dauern)..."; \
  Flags: runhidden

Filename: "{app}\{#MyAppExeName}"; \
  Description: "OPN-Cockpit jetzt starten"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; venv liegt im Installations-Ordner und wird mit deinstalliert.
Type: filesandordners; Name: "{app}\.venv"

[Code]
function InitializeSetup(): Boolean;
var
  PythonInstalled: Boolean;
  UvInstalled: Boolean;
  ResultCode: Integer;
begin
  Result := True;
  PythonInstalled := Exec('python.exe', '--version', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
  if not PythonInstalled then begin
    MsgBox('Python 3.11 oder neuer ist erforderlich. '
      + 'Bitte installiere Python von python.org und starte den Installer erneut.',
      mbError, MB_OK);
    Result := False;
    Exit;
  end;
  UvInstalled := Exec('uv.exe', '--version', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
  if not UvInstalled then begin
    MsgBox('Das Paketwerkzeug "uv" ist erforderlich. '
      + 'Bitte installiere uv (https://docs.astral.sh/uv/) und starte den Installer erneut.',
      mbError, MB_OK);
    Result := False;
  end;
end;
