# Integration Tests (live)

Tests in diesem Verzeichnis können gegen eine **echte OPNsense-Test-Instanz**
ausgeführt werden. Sie sind mit dem Pytest-Marker `@pytest.mark.live` versehen
und werden im Standard-Lauf (`pytest -q`) übersprungen.

## Voraussetzungen

- Erreichbare OPNsense-Test-VM (Zielversion 26.1)
- API-Key + Secret eines Test-Accounts auf dieser Instanz
- Test-Gateway-Name, der für Routen-Tests verwendet werden darf

## Konfiguration

Die Test-Verbindungsdaten kommen aus Umgebungsvariablen, **niemals** committed:

```powershell
$env:OPN_TEST_HOST  = "https://opnsense-lab.example.local"
$env:OPN_TEST_KEY   = "<api-key>"
$env:OPN_TEST_SECRET= "<api-secret>"
$env:OPN_TEST_GATEWAY = "LAB_WAN_GW"
```

## Ausführen

```powershell
pytest -m live tests/integration
```

## Was hier nicht hingehört

- Unit-Tests gegen `httpx.MockTransport` (→ `tests/unit/core/`)
- Smoke-Tests der GUI (PySide6) — die laufen separat
- Property-basierte Validierungstests (→ `tests/unit/core/`)
