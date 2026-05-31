"""Tests fuer /api/files/browse (Folder-Picker im Single-User-Mode)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.web.server import create_app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(tmp_path))
    return TestClient(create_app())


class TestBrowseEndpoint:
    def test_empty_path_returns_drives_on_windows(
        self, client: TestClient,
    ) -> None:
        response = client.get("/api/files/browse")
        assert response.status_code == 200
        body = response.json()
        if os.name == "nt":
            # Drives sind sortiert, C: muesste in jeder Win-Umgebung dabei sein.
            assert body["current"] == ""
            assert body["parent"] is None
            kinds = {e["kind"] for e in body["entries"]}
            assert "drive" in kinds
        else:
            # Unix: Home-Verzeichnis
            assert body["current"]
            assert body["parent"] is None

    def test_lists_subdirs_for_concrete_path(
        self, client: TestClient, tmp_path: Path,
    ) -> None:
        sub = tmp_path / "subdir"
        sub.mkdir()
        (tmp_path / "noise.txt").write_text("hi", encoding="utf-8")
        response = client.get(f"/api/files/browse?path={tmp_path}")
        assert response.status_code == 200
        body = response.json()
        names = {e["name"] for e in body["entries"]}
        assert "subdir" in names
        # Nicht-Vault-Datei wird gefiltert.
        assert "noise.txt" not in names

    def test_includes_existing_vault_files(
        self, client: TestClient, tmp_path: Path,
    ) -> None:
        (tmp_path / "existing.opnvault").write_bytes(b"x")
        response = client.get(f"/api/files/browse?path={tmp_path}")
        body = response.json()
        vault_entries = [e for e in body["entries"] if e["kind"] == "vault"]
        assert any(e["name"] == "existing.opnvault" for e in vault_entries)

    def test_relative_path_rejected(self, client: TestClient) -> None:
        response = client.get("/api/files/browse?path=relative/dir")
        assert response.status_code == 400

    def test_missing_path_returns_404(
        self, client: TestClient, tmp_path: Path,
    ) -> None:
        gone = tmp_path / "nicht-existent-xyz"
        response = client.get(f"/api/files/browse?path={gone}")
        assert response.status_code == 404

    def test_parent_when_at_root_is_drive_list(
        self, client: TestClient, tmp_path: Path,
    ) -> None:
        if os.name != "nt":
            pytest.skip("Drive-Liste nur unter Windows relevant.")
        response = client.get("/api/files/browse?path=C:\\")
        assert response.status_code == 200
        body = response.json()
        assert body["parent"] == ""

    def test_blocked_in_multi_user_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("OPNCOCKPIT_AUTH_BACKEND", "user-db")
        multi_client = TestClient(create_app())
        response = multi_client.get("/api/files/browse")
        assert response.status_code == 403
