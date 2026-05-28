"""Clientseitige Validierung für Aktionsparameter.

Wird vom Planner (Schritt 6) **vor** dem Erzeugen der API-Payloads aufgerufen.
Liefert klare, in der UI anzeigbare Fehlermeldungen, die ohne API-Round-Trip
auskommen.

Hält sich an die bekannten OPNsense-Regeln; die abschließende Korrektheit der
Feldwerte wird durch die API selbst entschieden und ggf. als
``ValidationError`` aus dem ``http_client`` weitergereicht.
"""

from __future__ import annotations

import ipaddress
import re

from opn_cockpit.core.errors import ValidationError, make_context

# ---------------------------------------------------------------------------
# CIDR / Routen
# ---------------------------------------------------------------------------

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_cidr(value: str) -> IPNetwork:
    """Parst eine CIDR-Notation und liefert das Netzwerk-Objekt.

    Lehnt Host-Bits außerhalb der Maske ab (``strict=True``) — die UI soll den
    Admin sofort sehen lassen, wenn er ``10.1.2.5/24`` statt ``10.1.2.0/24``
    eingegeben hat.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            "CIDR-Netz darf nicht leer sein.",
            context=make_context(error_kind="cidr_empty"),
        )
    cleaned = value.strip()
    try:
        return ipaddress.ip_network(cleaned, strict=True)
    except ValueError as exc:
        raise ValidationError(
            f"Ungültiges CIDR-Netz '{cleaned}': {exc}",
            context=make_context(error_kind="cidr_invalid", summary=str(exc)),
        ) from exc


# ---------------------------------------------------------------------------
# Aliase
# ---------------------------------------------------------------------------

# OPNsense-Aliasnamen: müssen mit einem Buchstaben beginnen, danach
# Buchstaben/Ziffern/Underscore, Gesamtlänge bis 32 Zeichen.
# (Endgültige Verifikation kommt mit dem API-Spike — Konstante zentral, leicht
# anpassbar.)
ALIAS_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
ALIAS_NAME_MAX_LEN = 32

# Liste lehnt sich an die OPNsense-26.x-Aliastypen an. Final mit Spike abgleichen.
ALLOWED_ALIAS_TYPES: frozenset[str] = frozenset(
    {
        "host",
        "network",
        "port",
        "url",
        "urltable",
        "geoip",
        "networkgroup",
        "mac",
        "asn",
        "dynipv6host",
        "authgroup",
        "internal",
        "external",
    }
)


def validate_alias_name(name: str) -> str:
    """Validiert einen Alias-Namen und gibt ihn (unverändert) zurück."""
    if not isinstance(name, str):
        raise ValidationError(
            "Alias-Name muss ein String sein.",
            context=make_context(error_kind="alias_name_type"),
        )
    stripped = name.strip()
    if not stripped:
        raise ValidationError(
            "Alias-Name darf nicht leer sein.",
            context=make_context(error_kind="alias_name_empty"),
        )
    if len(stripped) > ALIAS_NAME_MAX_LEN:
        raise ValidationError(
            f"Alias-Name '{stripped}' überschreitet {ALIAS_NAME_MAX_LEN} Zeichen.",
            context=make_context(error_kind="alias_name_too_long"),
        )
    if not ALIAS_NAME_PATTERN.fullmatch(stripped):
        raise ValidationError(
            f"Alias-Name '{stripped}' enthält unzulässige Zeichen "
            "(erlaubt: Buchstabe als erstes Zeichen, dann Buchstabe/Ziffer/Underscore).",
            context=make_context(error_kind="alias_name_pattern"),
        )
    return stripped


def validate_alias_type(type_value: str) -> str:
    """Validiert den Alias-Typ gegen die Whitelist und normalisiert auf lowercase."""
    if not isinstance(type_value, str) or not type_value.strip():
        raise ValidationError(
            "Alias-Typ darf nicht leer sein.",
            context=make_context(error_kind="alias_type_empty"),
        )
    lowered = type_value.strip().lower()
    if lowered not in ALLOWED_ALIAS_TYPES:
        raise ValidationError(
            f"Unbekannter Alias-Typ '{type_value}'. Erlaubt: "
            f"{sorted(ALLOWED_ALIAS_TYPES)}",
            context=make_context(error_kind="alias_type_unknown"),
        )
    return lowered


# ---------------------------------------------------------------------------
# Gateway-Name (Routen)
# ---------------------------------------------------------------------------

# OPNsense referenziert Gateways in der Routen-API über den Namen.
# Konvention im laufenden System: Buchstaben/Ziffern/Underscore, case-sensitive.
GATEWAY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def validate_gateway_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValidationError(
            "Gateway-Name darf nicht leer sein.",
            context=make_context(error_kind="gateway_name_empty"),
        )
    cleaned = name.strip()
    if not GATEWAY_NAME_PATTERN.fullmatch(cleaned):
        raise ValidationError(
            f"Gateway-Name '{cleaned}' enthält unzulässige Zeichen.",
            context=make_context(error_kind="gateway_name_pattern"),
        )
    return cleaned
