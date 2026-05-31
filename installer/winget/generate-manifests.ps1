<#
.SYNOPSIS
    Generiert winget-Manifeste aus den Vorlagen unter template/.

.DESCRIPTION
    Liest die drei YAML-Templates, ersetzt die Platzhalter ({{VERSION}},
    {{INSTALLER_URL}}, {{INSTALLER_SHA256}}, {{RELEASE_DATE}}) und schreibt
    die fertigen Manifeste nach out/manifests/l/ludwig-systems/opn-cockpit/<Version>/.

    Dieses Verzeichnis kann dann 1:1 in einen Fork von
    microsoft/winget-pkgs kopiert und per PR eingereicht werden.

.PARAMETER Version
    Versionsnummer ohne v-Praefix, z. B. 0.7.0.

.PARAMETER InstallerUrl
    Vollstaendige Download-URL der Installer-EXE auf GitHub-Releases.

.PARAMETER InstallerSha256
    SHA256-Hash der EXE (Hex, untere oder grosse Schreibweise — wird
    normalisiert auf Grossbuchstaben fuer winget-Schema-Konformitaet).

.PARAMETER ReleaseDate
    Optional; default ist heute (UTC) im YYYY-MM-DD-Format.

.EXAMPLE
    .\generate-manifests.ps1 `
        -Version 0.7.0 `
        -InstallerUrl "https://github.com/ludwig-systems/opn-cockpit/releases/download/v0.7.0/OPN-Cockpit-Setup-0.7.0.exe" `
        -InstallerSha256 "ABCDEF..."
#>

#requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Version,

    [Parameter(Mandatory)]
    [string]$InstallerUrl,

    [Parameter(Mandatory)]
    [string]$InstallerSha256,

    [string]$ReleaseDate = (Get-Date -AsUTC).ToString("yyyy-MM-dd")
)

$ErrorActionPreference = "Stop"

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$TemplateDir = Join-Path $ScriptDir "template"
$OutRoot     = Join-Path $ScriptDir "out\manifests\l\ludwig-systems\opn-cockpit\$Version"

if (-not (Test-Path $TemplateDir)) {
    throw "Template-Verzeichnis fehlt: $TemplateDir"
}

if (Test-Path $OutRoot) {
    Write-Host "==> Entferne alte Ausgabe: $OutRoot" -ForegroundColor Yellow
    Remove-Item -Recurse -Force $OutRoot
}
New-Item -ItemType Directory -Path $OutRoot -Force | Out-Null

# winget erwartet den Hash in Uppercase ohne Trennzeichen.
$normalizedHash = $InstallerSha256.Trim().ToUpperInvariant()

$replacements = @{
    "{{VERSION}}"          = $Version
    "{{INSTALLER_URL}}"    = $InstallerUrl
    "{{INSTALLER_SHA256}}" = $normalizedHash
    "{{RELEASE_DATE}}"     = $ReleaseDate
}

$generated = @()
foreach ($template in Get-ChildItem -Path $TemplateDir -Filter "*.yaml") {
    $content = Get-Content $template.FullName -Raw
    foreach ($key in $replacements.Keys) {
        $content = $content.Replace($key, $replacements[$key])
    }
    $outPath = Join-Path $OutRoot $template.Name
    Set-Content -Path $outPath -Value $content -Encoding utf8
    $generated += $outPath
    Write-Host "==> $outPath"
}

Write-Host ""
Write-Host "Fertig — $($generated.Count) Manifest(e) in $OutRoot." -ForegroundColor Green
Write-Host "Naechste Schritte:" -ForegroundColor Green
Write-Host "  1. Fork von microsoft/winget-pkgs auschecken"
Write-Host "  2. Ordner nach <Fork>/manifests/l/ludwig-systems/opn-cockpit/$Version/ kopieren"
Write-Host "  3. Lokal pruefen: winget validate <Fork>/manifests/l/ludwig-systems/opn-cockpit/$Version"
Write-Host "  4. PR gegen microsoft/winget-pkgs:master eroeffnen"
