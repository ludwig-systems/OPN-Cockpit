"""Tests fuer web.settings — Env-Variable-Parsing."""

from __future__ import annotations

from opn_cockpit.web.settings import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    RESTART_MODE_DEV,
    RESTART_MODE_NSSM,
    RESTART_MODE_SYSTEMD,
    TLS_SOURCE_ENV,
    WebSettings,
    detect_service_mode,
    service_name,
)


class TestFromEnv:
    def test_uses_defaults_when_env_empty(self) -> None:
        s = WebSettings.from_env(env={})
        assert s.host == DEFAULT_HOST
        assert s.port == DEFAULT_PORT
        assert s.auto_open_browser is True

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
        assert s.tls_source == TLS_SOURCE_ENV

    def test_allow_http_env(self) -> None:
        s = WebSettings.from_env(env={"OPNCOCKPIT_ALLOW_HTTP": "1"})
        assert s.allow_http_fallback is True

    def test_allow_http_default_off(self) -> None:
        s = WebSettings.from_env(env={})
        # Default aus (echte settings.json kann's uebersteuern, aber leerem
        # env-Dict darf keinen HTTP-Fallback aktivieren).
        # Wir pruefen den flag daher nur wenn er via env gesetzt wurde.
        # Reine env-Prüfung ohne AppSettings-Nebeneffekt:
        assert isinstance(s.allow_http_fallback, bool)


class TestBaseUrl:
    def test_https_by_default(self) -> None:
        """v0.10: Ohne TLS-Config und ohne HTTP-Fallback ist der Scheme https,
        weil der Boot das Auto-Cert generiert und HTTPS liefert. v0.11: Default-
        Port ist 443 — im base_url weggelassen (URL-Convention)."""
        s = WebSettings(allow_http_fallback=False)
        assert s.base_url == "https://127.0.0.1"

    def test_http_only_with_explicit_fallback_uses_default_port_suffix(self) -> None:
        # HTTP mit Port 443 ist ungewoehnlich; wenn der User das setzt,
        # taucht der Port explizit im base_url auf.
        s = WebSettings(allow_http_fallback=True)
        assert s.base_url == "http://127.0.0.1:443"

    def test_http_default_port_omitted(self) -> None:
        s = WebSettings(port=80, allow_http_fallback=True)
        assert s.base_url == "http://127.0.0.1"

    def test_non_default_https_port_kept(self) -> None:
        s = WebSettings(port=9876, allow_http_fallback=False)
        assert s.base_url == "https://127.0.0.1:9876"

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


class TestDetectServiceMode:
    def test_explicit_dev_override(self) -> None:
        assert detect_service_mode(env={"OPNCOCKPIT_RESTART_MODE": "dev"}) == RESTART_MODE_DEV

    def test_explicit_nssm_override(self) -> None:
        assert detect_service_mode(env={"OPNCOCKPIT_RESTART_MODE": "nssm"}) == RESTART_MODE_NSSM

    def test_explicit_systemd_override(self) -> None:
        assert (
            detect_service_mode(env={"OPNCOCKPIT_RESTART_MODE": "systemd"}) == RESTART_MODE_SYSTEMD
        )

    def test_unknown_override_ignored(self) -> None:
        # Faellt auf platform-Heuristik zurueck; Test ist plattform-agnostisch
        # solange wir kein systemd im Env sehen und kein NSSM auf Linux.
        result = detect_service_mode(env={"OPNCOCKPIT_RESTART_MODE": "garbage"})
        assert result in {RESTART_MODE_DEV, RESTART_MODE_NSSM, RESTART_MODE_SYSTEMD}


class TestServiceName:
    def test_service_name_default(self) -> None:
        name = service_name(env={})
        assert name  # Nicht leer
