"""Tests fuer _redirect_stdio_if_windowless im web/runner.py.

Wir koennen pythonw nicht im Test simulieren - aber das Verhalten
(Detect via sys.stdout==None + Redirect zu Log-Datei) ist mit
monkeypatch testbar.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import pytest

from opn_cockpit.web.runner import _redirect_stdio_if_windowless


class TestRedirectStdioIfWindowless:
    def test_no_redirect_when_stdout_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Normaler Konsolen-Start: keine Aenderung an sys.stdout/stderr."""
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        monkeypatch.setenv("APPDATA", str(tmp_path))
        _redirect_stdio_if_windowless()
        assert sys.stdout is original_stdout
        assert sys.stderr is original_stderr
        # Insbesondere wurde KEIN logs/-Verzeichnis angelegt
        assert not (tmp_path / "OPN-Cockpit" / "logs").exists()

    def test_redirect_when_stdout_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Windowless-Mode (pythonw): stdout/stderr -> log-file."""
        monkeypatch.setenv("APPDATA", str(tmp_path))
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)
        try:
            _redirect_stdio_if_windowless()
            # Beide muessen jetzt auf dieselbe File-Handle zeigen
            assert sys.stdout is not None
            assert sys.stderr is not None
            assert sys.stdout is sys.stderr
            # Datei existiert + enthaelt den Boot-Marker
            log_path = tmp_path / "OPN-Cockpit" / "logs" / "opn-cockpit.log"
            assert log_path.exists()
            content = log_path.read_text(encoding="utf-8")
            assert "Windowless-Start" in content
        finally:
            # Cleanup: handle schliessen damit pytest-tempdir loeschen kann
            if sys.stdout is not None and hasattr(sys.stdout, "close"):
                with contextlib.suppress(OSError):
                    sys.stdout.close()

    def test_redirect_survives_log_dir_creation_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Wenn die Log-Datei nicht angelegt werden kann: schweige, crashe nicht."""
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)

        def boom(*_a: object, **_kw: object) -> Path:
            raise OSError("simulated permission denied")

        monkeypatch.setattr("opn_cockpit.config.get_app_data_dir", boom)
        # Darf NICHT crashen
        _redirect_stdio_if_windowless()
