# Vendored Third-Party Binaries

In diesem Verzeichnis liegen kleine, externe Binaries, die der Windows-
Installer braucht — direkt im Repo, damit das Build deterministisch und
**unabhängig von externen Download-Servern** ist.

## nssm.exe

**NSSM** (Non-Sucking Service Manager) — wickelt im Multi-User-Server-Mode
unsere Python-Anwendung als Windows-Dienst. Wird beim Setup-Wizard
„Multi-User-Server" registriert.

| Feld | Wert |
|---|---|
| Version | 2.24 (win64) |
| Quelle | <https://nssm.cc/release/nssm-2.24.zip> → `win64/nssm.exe` |
| Größe | 368 640 Bytes |
| SHA256 | `eee9c44c29c2be011f1f1e43bb8c3fca888cb81053022ec5a0060035de16d848` |
| Build-Zeit | 2017-04-26 (vendor-time, kein Build der offiziellen 2.24-Release) |
| Lizenz | Public Domain (siehe unten) |

### Lizenz / Nutzungsbedingung

Aus dem NSSM-Header: „NSSM is public domain. You may use it for any
purpose whatsoever, but at your own risk." Keine Attribution erforderlich,
keine Beschränkung der Nutzung.

### Verifikation

Hash-Check bei jedem Build (Pre-Step im Workflow oder lokal):

```powershell
(Get-FileHash installer\vendor\nssm.exe -Algorithm SHA256).Hash
# muss obigen Wert ergeben
```

### Warum nicht beim Build runterladen?

`nssm.cc` ist ein Single-Operator-Server (Iain Patterson) und hat
regelmäßig kurze Aussetzer (HTTP 503). Build-Pipeline blieb mehrfach
hängen. Eine 360-KB-Datei einmal vendord macht das Build deterministisch
und überlebt nssm.cc-Downtimes.

### Update-Prozess

Sollte irgendwann eine neuere NSSM-Version raus sein:
1. Original-ZIP von <https://nssm.cc/download> ziehen
2. `win64/nssm.exe` extrahieren
3. Diese Datei hier ersetzen
4. Neuen SHA256 in dieser README eintragen
5. Build durchlaufen lassen + manuell Service-Mode-Install testen
