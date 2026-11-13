"""Tests fuer den FastAPI Server-Skeleton."""

from __future__ import annotations

from fastapi.testclient import TestClient

from opn_cockpit import __version__
from opn_cockpit.web.server import create_app


def _client() -> TestClient:
    return TestClient(create_app())


class TestHealth:
    def test_returns_ok(self) -> None:
        with _client() as c:
            response = c.get("/health")
        assert response.status_code == 200
        assert response.text == "ok"

    def test_no_openapi_exposed(self) -> None:
        # docs_url + redoc_url + openapi_url sind alle deaktiviert
        with _client() as c:
            assert c.get("/docs").status_code == 404
            assert c.get("/redoc").status_code == 404
            assert c.get("/openapi.json").status_code == 404


class TestVersionEndpoint:
    def test_returns_current_version(self) -> None:
        with _client() as c:
            response = c.get("/api/version")
        assert response.status_code == 200
        assert response.json() == {"version": __version__}


class TestIndex:
    def test_serves_html_with_version_meta(self) -> None:
        with _client() as c:
            response = c.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "OPN-Cockpit" in response.text
        assert __version__ in response.text

    def test_loads_app_js_and_styles(self) -> None:
        with _client() as c:
            response = c.get("/")
        body = response.text
        assert "/static/styles.css" in body
        assert "/static/app.js" in body


class TestStatic:
    def test_serves_stylesheet(self) -> None:
        with _client() as c:
            response = c.get("/static/styles.css")
        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]

    def test_serves_app_js(self) -> None:
        with _client() as c:
            response = c.get("/static/app.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
