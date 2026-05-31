"""Tests fuer /api/about + About-Modal-Markup."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opn_cockpit import (
    __author__,
    __author_email__,
    __github_url__,
    __version__,
)
from opn_cockpit.web.server import create_app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(tmp_path))
    return TestClient(create_app())


class TestAboutEndpoint:
    def test_returns_version_and_metadata(self, client: TestClient) -> None:
        response = client.get("/api/about")
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "OPN-Cockpit"
        assert body["version"] == __version__
        assert body["author"] == __author__
        assert body["author_email"] == __author_email__
        assert body["github_url"] == __github_url__
        assert body["license"]

    def test_is_anonymous(self, client: TestClient) -> None:
        """About darf ohne Auth aufgerufen werden — keine 401/403."""
        response = client.get("/api/about")
        assert response.status_code == 200


class TestAboutModalMarkup:
    def test_index_includes_about_modal_skeleton(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        html = response.text
        assert 'id="about-modal"' in html
        assert 'id="about-open-btn"' in html
        assert 'id="about-version"' in html
        assert 'id="about-github"' in html
