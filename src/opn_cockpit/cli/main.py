"""Headless-CLI für OPN-Cockpit (Schritt 6 des Umsetzungsplans).

Aktuell nur ein Platzhalter, der die geplante Sub-Command-Struktur dokumentiert
und mit einem klaren Hinweis terminiert. Die echte Implementierung folgt mit
Schritt 6 (Orchestrierung + Headless-End-to-End).
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opn-cockpit-cli",
        description="OPN-Cockpit Headless-CLI (in Vorbereitung).",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("list-devices", help="Inventarisierte Geräte auflisten")
    sub.add_parser("test-connection", help="Erreichbarkeit + Auth eines Geräts prüfen")
    sub.add_parser("plan", help="Aktions-Vorschau erzeugen (Dry-Run)")
    sub.add_parser("apply", help="Vorher erzeugte Vorschau ausrollen")
    sub.add_parser("audit", help="Audit-Log filtern und anzeigen")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 1
    sys.stderr.write(
        f"Sub-Command '{args.command}' ist noch nicht implementiert "
        "(siehe Plan Schritt 6).\n"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
