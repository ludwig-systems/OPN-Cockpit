"""Tests fuer web.settings — Env-Variable-Parsing."""

from __future__ import annotations

from opn_cockpit.web.settings import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    WebSettings,
)


class TestFromEnv:
    def test_uses_defaults_when_env_empty(self) -> None:
        s = WebSettings.from_env(env={})
        assert s.host == DEFAULT_HOST
        assert s.port == DEFAULT_PORT
        assert s.auto_open_browser is True
        assert s.tls_cert is None
        assert s.tls_key is None

    def test_reads_host_and_port(self) -> None:
        s = WebSettings.from_env(env={"OPNCOCKPIT_HOST": "0.0.0.0", "OPNCOCKPIT_PORT": "5050"})
        assert s.host == "0.0.0.0"
        assert s.port == 5050

    def test_invalid_port_falls_back_to_default(self) -> None:
        s = WebSettings.from_env(env={"OPNCOCKPIT_PORT": "not-a-number"})
        assert s.port == DEFAULT_PORT

    def test_no_browser_flag(self) -> None:
        s = WebSettings.from_env(env={"OPNCOCKPIT_NO_BROWSER": "1"})
        assert s.auto_open_browser is False

    def test_tls_settings(self) -> None:
        s = WebSettings.from_env(
            env={"OPNCOCKPIT_TLS_CERT": "/path/cert.pem", "OPNCOCKPIT_TLS_KEY": "/path/key.pem"}
        )
        assert s.tls_cert == "/path/cert.pem"
        assert s.tls_key == "/path/key.pem"


class TestBaseUrl:
    def test_http_for_loopback_without_tls(self) -> None:
        s = WebSettings()
        assert s.base_url == "http://127.0.0.1:9876"

    def test_https_when_tls_configured(self) -> None:
        s = WebSettings(tls_cert="/c", tls_key="/k")
        assert s.base_url.startswith("https://")


class TestIsLoopbackOnly:
    def test_default_is_loopback(self) -> None:
        assert WebSettings().is_loopback_only is True

    def test_zero_zero_zero_zero_is_not(self) -> None:
        assert WebSettings(host="0.0.0.0").is_loopback_only is False

    def test_localhost_counts(self) -> None:
        assert WebSettings(host="localhost").is_loopback_only is True
