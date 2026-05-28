"""Entry-Point für ``python -m opn_cockpit`` — startet die PySide6-GUI."""

from __future__ import annotations

from opn_cockpit.gui.app import run


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
