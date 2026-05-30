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


# ---------------------------------------------------------------------------
# Host (IPv4/IPv6 oder DNS-Hostname)
# ---------------------------------------------------------------------------

# RFC-1035-konformes Hostname-Label: ein bis 63 Zeichen, Buchstaben/Ziffern/
# Bindestrich, beginnt und endet nicht mit Bindestrich. FQDN: Labels durch
# Punkte getrennt, max. 253 Zeichen gesamt.
_HOSTNAME_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
HOSTNAME_MAX_LEN = 253
PORT_MIN = 1
PORT_MAX = 65535


def validate_host(value: str) -> str:
    """Validiert einen Host (IPv4, IPv6 oder DNS-Hostname / FQDN).

    Liefert den getrimmten Wert. Wirft ``ValidationError`` bei
    ungueltigen Eingaben. Akzeptiert:

    * IPv4: ``10.0.0.1``
    * IPv6: ``2001:db8::1`` (auch in eckigen Klammern)
    * Hostname: ``opn-1.lab``, ``hq-berlin``
    """
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            "Hostname / IP darf nicht leer sein.",
            context=make_context(error_kind="host_empty"),
        )
    cleaned = value.strip()
    # IPv6 oft in eckigen Klammern in URLs — wir akzeptieren das
    cleaned_inner = (
        cleaned[1:-1]
        if cleaned.startswith("[") and cleaned.endswith("]")
        else cleaned
    )
    # IP-Versuch zuerst
    try:
        ipaddress.ip_address(cleaned_inner)
        return cleaned
    except ValueError:
        pass
    # Dann Hostname
    if len(cleaned) > HOSTNAME_MAX_LEN:
        raise ValidationError(
            f"Hostname '{cleaned}' ueberschreitet {HOSTNAME_MAX_LEN} Zeichen.",
            context=make_context(error_kind="host_too_long"),
        )
    labels = cleaned.rstrip(".").split(".")
    for label in labels:
        if not _HOSTNAME_LABEL.fullmatch(label):
            raise ValidationError(
                f"'{cleaned}' ist weder eine gueltige IP noch ein gueltiger Hostname.",
                context=make_context(error_kind="host_pattern"),
            )
    return cleaned


# ---------------------------------------------------------------------------
# Port-Range (fuer Alias-Content vom Typ "port")
# ---------------------------------------------------------------------------

_PORT_RANGE_RE = re.compile(r"^(\d{1,5})(?:[-:](\d{1,5}))?$")


def validate_port_value(value: str) -> str:
    """Akzeptiert eine Port-Zahl (1-65535) oder eine Range (``80-90``, ``1024:2048``)."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            "Port-Wert darf nicht leer sein.",
            context=make_context(error_kind="port_empty"),
        )
    cleaned = value.strip()
    m = _PORT_RANGE_RE.fullmatch(cleaned)
    if m is None:
        raise ValidationError(
            f"Port-Wert '{cleaned}' nicht erkannt (erwartet: 1-65535 oder Range).",
            context=make_context(error_kind="port_pattern"),
        )
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    if not (PORT_MIN <= start <= PORT_MAX and PORT_MIN <= end <= PORT_MAX):
        raise ValidationError(
            f"Port '{cleaned}' ausserhalb des Bereichs {PORT_MIN}-{PORT_MAX}.",
            context=make_context(error_kind="port_out_of_range"),
        )
    if end < start:
        raise ValidationError(
            f"Port-Range '{cleaned}': End-Port < Start-Port.",
            context=make_context(error_kind="port_range_reversed"),
        )
    return cleaned


# ---------------------------------------------------------------------------
# URL (fuer Alias-Content vom Typ "url" / "urltable")
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


def validate_url(value: str) -> str:
    """Minimaler URL-Check: http/https-Schema, kein Whitespace."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            "URL darf nicht leer sein.",
            context=make_context(error_kind="url_empty"),
        )
    cleaned = value.strip()
    if not _URL_RE.fullmatch(cleaned):
        raise ValidationError(
            f"'{cleaned}' ist keine gueltige http/https-URL.",
            context=make_context(error_kind="url_pattern"),
        )
    return cleaned


# ---------------------------------------------------------------------------
# Alias-Content (typabhaengige Validierung der Listen-Eintraege)
# ---------------------------------------------------------------------------


def validate_alias_content(alias_type: str, content: list[str] | tuple[str, ...]) -> list[str]:
    """Validiert die Inhalte eines Alias gegen den deklarierten Typ.

    * ``host``: IPv4/IPv6 oder Hostname
    * ``network``: CIDR
    * ``port``: Port oder Port-Range
    * ``url`` / ``urltable``: http/https-URL
    * andere Typen (mac, asn, geoip, ...) werden heute nicht
      strikt validiert — nur Leer-Check, weil die genaue Form je nach
      OPNsense-Version variiert. Spaeter koennte man hier ausbauen.

    Liefert die getrimmten Werte. Wirft ``ValidationError`` mit klarer
    Meldung bei der ersten verletzenden Zeile (inkl. Index).
    """
    if not content:
        raise ValidationError(
            "Mindestens ein Alias-Eintrag erforderlich.",
            context=make_context(error_kind="alias_content_empty"),
        )
    t = (alias_type or "").strip().lower()
    cleaned: list[str] = []
    for i, raw in enumerate(content):
        v = (raw or "").strip()
        if not v:
            continue
        try:
            if t == "host":
                cleaned.append(validate_host(v))
            elif t == "network":
                parse_cidr(v)
                cleaned.append(v)
            elif t == "port":
                cleaned.append(validate_port_value(v))
            elif t in ("url", "urltable"):
                cleaned.append(validate_url(v))
            else:
                # Unbekannter Typ → minimal akzeptieren
                cleaned.append(v)
        except ValidationError as exc:
            # Fehler mit Position ergaenzen
            raise ValidationError(
                f"Alias-Eintrag #{i + 1}: {exc}",
                context=make_context(
                    error_kind="alias_content_invalid",
                    summary=str(exc),
                ),
            ) from exc
    if not cleaned:
        raise ValidationError(
            "Alle Alias-Eintraege sind leer.",
            context=make_context(error_kind="alias_content_all_empty"),
        )
    return cleaned
