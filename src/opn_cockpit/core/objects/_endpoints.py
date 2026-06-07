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
# Unbound-DNS Host-Overrides (core OPNsense, kein Plugin noetig)
# ---------------------------------------------------------------------------

UNBOUND_HOST_ADD = "/api/unbound/settings/addHostOverride"
UNBOUND_HOST_SEARCH = "/api/unbound/settings/searchHostOverride"
UNBOUND_HOST_GET = "/api/unbound/settings/getHostOverride/{uuid}"
UNBOUND_HOST_SET = "/api/unbound/settings/setHostOverride/{uuid}"
UNBOUND_HOST_DEL = "/api/unbound/settings/delHostOverride/{uuid}"
UNBOUND_RECONFIGURE = "/api/unbound/service/reconfigure"

# Unbound-DNS Domain-Overrides (Weiterleitungen): leiten alle Queries fuer
# eine Domain an einen externen Resolver weiter. Read-only-Anzeige (CRUD
# kann spaeter folgen).
UNBOUND_DOMAIN_SEARCH = "/api/unbound/settings/searchDomainOverride"
UNBOUND_DOMAIN_GET = "/api/unbound/settings/getDomainOverride/{uuid}"

# Unbound-DNS Query-Forwards (UI-Tab "Query Forwarding"): die globalen
# Forward-Server (oft DoT/DoH), an die ALLE Queries (oder fuer eine
# bestimmte Domain) weitergegeben werden. Read-only-Anzeige.
UNBOUND_FORWARD_SEARCH = "/api/unbound/settings/searchForward"
UNBOUND_FORWARD_GET = "/api/unbound/settings/getForward/{uuid}"

# ---------------------------------------------------------------------------
# Discovery (v1.1)
# ---------------------------------------------------------------------------

GATEWAY_STATUS = "/api/routes/gateway/status"
