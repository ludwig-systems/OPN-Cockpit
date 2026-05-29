"""Entry-Point für ``python -m opn_cockpit`` — startet den Web-Server."""

from __future__ import annotations

from opn_cockpit.web.runner import run


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
