"""Erlaubt ``python -m opn_cockpit.cli`` zusätzlich zum Entry-Point ``opn-cockpit-cli``."""

from opn_cockpit.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
