"""Zentrale OPNsense-REST-API-Pfade pro Subsystem.

Ein Modul mit Konstanten, damit bei API-Pfadwechseln zwischen OPNsense-
Versionen genau eine Stelle anzupassen ist. Mit Schritt 0 (API-Spike) werden
diese Werte gegen die laufende 26.1-Instanz verifiziert.

Versionsstand: an OPNsense 26.1 ausgerichtet, abschließende Bestätigung
folgt mit dem API-Spike (siehe ``docs/opnsense-api-26.1.md``).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------

ROUTES_ADD = "/api/routes/routes/addroute"
ROUTES_SEARCH = "/api/routes/routes/searchroute"
ROUTES_GET = "/api/routes/routes/getroute/{uuid}"
ROUTES_SET = "/api/routes/routes/setroute/{uuid}"
ROUTES_DEL = "/api/routes/routes/delroute/{uuid}"
ROUTES_RECONFIGURE = "/api/routes/routes/reconfigure"

# ---------------------------------------------------------------------------
# Aliase (Firewall)
# ---------------------------------------------------------------------------

ALIAS_ADD = "/api/firewall/alias/addItem"
ALIAS_SEARCH = "/api/firewall/alias/searchItem"
ALIAS_GET = "/api/firewall/alias/getItem/{uuid}"
ALIAS_SET = "/api/firewall/alias/setItem/{uuid}"
ALIAS_DEL = "/api/firewall/alias/delItem/{uuid}"
ALIAS_RECONFIGURE = "/api/firewall/alias/reconfigure"

# ---------------------------------------------------------------------------
# Firewall-Regeln (os-firewall Plugin, Standard ab OPNsense 24+)
# ---------------------------------------------------------------------------

RULE_ADD = "/api/firewall/filter/addRule"
RULE_SEARCH = "/api/firewall/filter/searchRule"
RULE_GET = "/api/firewall/filter/getRule/{uuid}"
RULE_SET = "/api/firewall/filter/setRule/{uuid}"
RULE_DEL = "/api/firewall/filter/delRule/{uuid}"
RULE_APPLY = "/api/firewall/filter/apply"

# ---------------------------------------------------------------------------
# Discovery (v1.1)
# ---------------------------------------------------------------------------

GATEWAY_STATUS = "/api/routes/gateway/status"
