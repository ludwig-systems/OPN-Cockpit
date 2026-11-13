"""Tests für orchestration.registry."""

from __future__ import annotations

import pytest

from opn_cockpit.core.objects.routes import RouteAdapter, RoutesController
from opn_cockpit.orchestration.registry import get_binding, known_subsystems


class TestRegistry:
    def test_routes_binding_exposes_adapter_and_controller(self) -> None:
        binding = get_binding("routes")
        assert isinstance(binding.adapter, RouteAdapter)
        assert isinstance(binding.controller, RoutesController)

    def test_unknown_subsystem_raises(self) -> None:
        with pytest.raises(KeyError):
            get_binding("does-not-exist")

    def test_known_subsystems_includes_routes(self) -> None:
        assert "routes" in known_subsystems()
