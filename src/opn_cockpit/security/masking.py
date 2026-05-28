"""Maskierung von Geheimnissen für Logs, Vorschauen und Crash-Reports.

Verbindlich (R-SEC-5, R-LOG-3): Klartext-Secrets dürfen niemals in Audit-Log,
GUI-Vorschau oder Crash-Output landen. Dieses Modul ist die zentrale
Disziplinquelle dafür.

Schlüsselregel: ``MaskedStr`` ist **kein** ``str``-Subclass. Eine
String-Subclass würde bei ``f"{m}"`` und ``"%s" % m`` lautlos den Klartext
freigeben, weil Format-Operatoren auf der zugrunde liegenden Stringklasse
arbeiten. Stattdessen ist ``MaskedStr`` ein eigener Typ, der **alle**
String-Conversion-Pfade (``__str__``, ``__repr__``, ``__format__``)
überschreibt und für die Konkatenation mit ``str`` absichtlich keine
``__add__``-Methode definiert (führt zu ``TypeError`` und macht damit
versehentliche Verkettung sichtbar).
"""

from __future__ import annotations

import re
from typing import Any, Final

MASK_TOKEN: Final[str] = "***"

# Erkennt verbreitete Schlüsselnamen, die typischerweise Geheimnisse halten.
# Das Tool bleibt streng — lieber zu viel maskieren als zu wenig. Erweiterbar
# via ``mask_dict(..., pattern=...)`` für Spezialfälle.
DEFAULT_SECRET_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(secret|password|api[_-]?key|token|authorization)",
    re.IGNORECASE,
)


class MaskedStr:
    """Wert, der sich nicht versehentlich als Klartext serialisieren lässt.

    Echter Wert nur über :meth:`reveal` abrufbar — dieser Methodenname taucht
    in jedem Audit deutlich sichtbar auf und macht Reveal-Aufrufe leicht
    findbar.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Gibt den Klartext zurück. Verwende sparsam und nur dort, wo es
        unausweichlich ist (z. B. beim Aufbau des HTTP-Basic-Auth-Headers)."""
        return self._value

    def __repr__(self) -> str:
        return MASK_TOKEN

    def __str__(self) -> str:
        return MASK_TOKEN

    def __format__(self, _spec: str) -> str:
        return MASK_TOKEN

    def __eq__(self, other: object) -> bool:
        if isinstance(other, MaskedStr):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("MaskedStr", self._value))

    def __bool__(self) -> bool:
        return bool(self._value)


def mask_secret(value: str) -> MaskedStr:
    """Wickelt einen Klartext in eine ``MaskedStr``."""
    return MaskedStr(value)


def mask_dict(
    payload: dict[str, Any],
    pattern: re.Pattern[str] = DEFAULT_SECRET_KEY_PATTERN,
) -> dict[str, Any]:
    """Liefert eine flache Kopie mit sensitiven Schlüsseln maskiert.

    Rekursiv: dicts und Listen werden mitbehandelt. ``MaskedStr``-Werte
    bleiben in Ruhe (sind ohnehin maskiert).
    """
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if isinstance(k, str) and pattern.search(k):
            out[k] = MASK_TOKEN
        else:
            out[k] = _mask_value(v, pattern)
    return out


def _mask_value(value: Any, pattern: re.Pattern[str]) -> Any:
    if isinstance(value, dict):
        return mask_dict(value, pattern)
    if isinstance(value, list):
        return [_mask_value(item, pattern) for item in value]
    if isinstance(value, tuple):
        return tuple(_mask_value(item, pattern) for item in value)
    return value
