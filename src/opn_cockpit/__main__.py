"""Entry-Point für ``python -m opn_cockpit``.

In v1 wird hier später die PySide6-GUI gestartet (Schritt 8 des Plans). Aktuell
ist nur das Projektgerüst vorhanden, daher endet der Aufruf mit einem klaren
Hinweis.
"""

from __future__ import annotations

import sys


def main() -> int:
    sys.stderr.write(
        "OPN-Cockpit GUI ist noch nicht implementiert (siehe Plan Schritt 8).\n"
        "Headless-CLI: python -m opn_cockpit.cli --help\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
