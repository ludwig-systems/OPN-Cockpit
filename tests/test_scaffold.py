"""Sentinel-Tests für das Projektgerüst.

Stellt sicher, dass das Package importierbar ist und die geplanten
Sub-Packages existieren. Wird durch echte Tests pro Schicht ersetzt, sobald
die Module Inhalt bekommen.
"""

from __future__ import annotations

import importlib

import pytest

import opn_cockpit
from opn_cockpit.cli.main import build_parser


@pytest.mark.parametrize(
    "module",
    [
        "opn_cockpit",
        "opn_cockpit.core",
        "opn_cockpit.core.objects",
        "opn_cockpit.orchestration",
        "opn_cockpit.inventory",
        "opn_cockpit.security",
        "opn_cockpit.audit",
        "opn_cockpit.profiles",
        "opn_cockpit.importers",
        "opn_cockpit.cli",
        "opn_cockpit.cli.main",
        "opn_cockpit.gui",
    ],
)
def test_package_importable(module: str) -> None:
    importlib.import_module(module)


def test_version_string() -> None:
    assert opn_cockpit.__version__ == "0.1.0"


def test_cli_parser_lists_planned_subcommands() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    for sub in ("list-devices", "test-connection", "plan", "apply", "audit"):
        assert sub in help_text
