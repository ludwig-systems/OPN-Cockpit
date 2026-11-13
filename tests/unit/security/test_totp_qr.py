"""Tests fuer den QR-SVG-Renderer."""

from __future__ import annotations

from opn_cockpit.security import totp as totp_mod


class TestRenderQrSvg:
    def test_returns_valid_svg_for_otpauth_uri(self) -> None:
        secret = totp_mod.generate_secret()
        uri = totp_mod.provisioning_uri(secret, "alice")
        svg = totp_mod.render_qr_svg(uri)
        assert svg.startswith("<svg") or svg.startswith("<?xml")
        assert "</svg>" in svg
        # Kein <script> drin — wir embedded das spaeter via innerHTML.
        assert "<script" not in svg.lower()
        # Kein inline-on... event-handler.
        assert "onload=" not in svg.lower()
        assert "onclick=" not in svg.lower()

    def test_returns_empty_string_on_render_failure(self) -> None:
        # Extrem langer Inhalt sprengt das QR-Cap (max ~4296 alphanum chars),
        # die qrcode-Library wirft ValueError → leerer String.
        too_long = "x" * 10_000
        svg = totp_mod.render_qr_svg(too_long)
        assert svg == ""

    def test_svg_size_is_compact(self) -> None:
        secret = totp_mod.generate_secret()
        uri = totp_mod.provisioning_uri(secret, "alice")
        svg = totp_mod.render_qr_svg(uri)
        # Sanity: typische otpauth-URI passt in <20 KB SVG (SvgPathImage).
        assert len(svg) < 20_000
