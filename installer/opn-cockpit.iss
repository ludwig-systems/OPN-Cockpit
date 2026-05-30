; Inno Setup Skript fuer OPN-Cockpit v3.2.
;
; Voraussetzung: Inno Setup 6+ (https://jrsoftware.org/isinfo.php).
; Build: ISCC opn-cockpit.iss   (oder per GUI)
;
; Was der Installer macht:
;   - Kopiert das Repo nach %ProgramFiles%\OPN-Cockpit\
;   - Installiert die Runtime-Dependencies via setup-venv.ps1
;   - Optional: Installiert OPN-Cockpit als Windows-Dienst (NSSM-basiert)
;   - Legt Desktop-Verknuepfung "OPN-Cockpit" an (im Single-Mode)
;   - Legt Start-Menue-Eintrag an
;   - Speichert Tresor-Dateien NICHT mit (die liegen in %APPDATA%)
;
; Aus dem Source-Tree kommen mit:
;   src\, scripts\, start.bat, README.md, docs\, CHANGELOG.md
;   bundle\nssm.exe  (nur wenn vor dem Build manuell heruntergeladen)
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
; Tree-Inhalt minus die nicht-relevanten Ordner.
; "..\" weil das Skript in installer\ liegt.
Source: "..\src\*"; DestDir: "{app}\src"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\scripts\setup-venv.ps1";        DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\demo_setup.py";         DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\install-service.ps1";   DestDir: "{app}\scripts"; Flags: ignoreversion; Components: service
Source: "..\scripts\uninstall-service.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion; Components: service
Source: "..\start.bat";              DestDir: "{app}"; Flags: ignoreversion
Source: "..\pyproject.toml";         DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md";              DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md";           DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\*";                 DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs
; NSSM-Bundle - Build-Schritt muss `installer\bundle\nssm.exe` vorlegen
; (Download von nssm.cc, public domain). Optional: existiert nur wenn
; Service-Komponente gewaehlt.
Source: "bundle\nssm.exe";           DestDir: "{app}\bundle"; Flags: ignoreversion skipifsourcedoesntexist; Components: service

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
; Nach dem Kopieren: venv anlegen + Dependencies installieren.
; Erwartet, dass auf dem Zielsystem Python 3.11+ und uv installiert sind.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\setup-venv.ps1"""; \
  WorkingDir: "{app}"; \
  StatusMsg: "Python-Umgebung wird eingerichtet (kann eine Minute dauern)..."; \
  Flags: runhidden

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
; venv liegt im Installations-Ordner und wird mit deinstalliert.
Type: filesandordners; Name: "{app}\.venv"

[Code]
const
  PYTHON_INSTALLER_URL = 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe';
  UV_INSTALLER_PS1     = 'https://astral.sh/uv/install.ps1';

function CheckExe(const ExeName: string): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExeName, '--version', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function DownloadPython(): Boolean;
var
  ResultCode: Integer;
  Target: string;
  Question: Integer;
begin
  Question := MsgBox(
    'Python 3.11 wurde nicht gefunden.' #13#10 #13#10
    + 'Soll der Installer Python 3.11.9 (64-bit) automatisch herunterladen'
    + ' und im "Just for me"-Modus installieren? (Empfohlen)' #13#10 #13#10
    + 'Bei "Nein" bricht der Installer ab.',
    mbConfirmation, MB_YESNO);
  if Question <> IDYES then begin
    Result := False;
    Exit;
  end;

  Target := ExpandConstant('{tmp}\python-installer.exe');
  if not Exec('powershell.exe',
    '-NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri ''' + PYTHON_INSTALLER_URL + ''' -OutFile ''' + Target + '''"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0)
  then begin
    MsgBox('Python-Download fehlgeschlagen. Bitte manuell von python.org installieren.',
      mbError, MB_OK);
    Result := False;
    Exit;
  end;

  // Silent install: per-user, PATH erweitern, ohne Test-Suite + Doc.
  if not Exec(Target,
    '/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_doc=0',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0)
  then begin
    MsgBox('Python-Installation fehlgeschlagen (Exit ' + IntToStr(ResultCode) + ').',
      mbError, MB_OK);
    Result := False;
    Exit;
  end;

  Result := True;
end;

function DownloadUv(): Boolean;
var
  ResultCode: Integer;
  Question: Integer;
begin
  Question := MsgBox(
    'Das Paketwerkzeug "uv" wurde nicht gefunden.' #13#10 #13#10
    + 'Soll der Installer uv automatisch herunterladen und installieren?' #13#10
    + '(Empfohlen — schnelle pip-Alternative von astral.sh)',
    mbConfirmation, MB_YESNO);
  if Question <> IDYES then begin
    Result := False;
    Exit;
  end;

  // Astral liefert ein PowerShell-One-Liner-Installer.
  if not Exec('powershell.exe',
    '-NoProfile -ExecutionPolicy Bypass -Command "irm ' + UV_INSTALLER_PS1 + ' | iex"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0)
  then begin
    MsgBox('uv-Installation fehlgeschlagen (Exit ' + IntToStr(ResultCode) + ').' #13#10
      + 'Bitte manuell installieren: https://docs.astral.sh/uv/',
      mbError, MB_OK);
    Result := False;
    Exit;
  end;

  Result := True;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;

  if not CheckExe('python.exe') then begin
    if not DownloadPython() then begin
      Result := False;
      Exit;
    end;
    // Nach dem Install ein zweites Mal pruefen — PATH wurde aktualisiert,
    // aber Inno-Setup-Subprozess hat das alte PATH-Cache. Trotzdem oft
    // schon nutzbar via vollem Pfad. Best-effort.
  end;

  if not CheckExe('uv.exe') then begin
    if not DownloadUv() then begin
      Result := False;
    end;
  end;
end;
