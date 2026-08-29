# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for NetworkController functional-test mode.

Covers the ``METIXEL_NETWORK_TEST_MODE`` seam: when enabled, Ethernet is
ignored for connectivity decisions so WiFi/AP functional tests can run while
the Pi stays reachable over Ethernet.  Production behaviour (flag absent) is
unchanged.
"""

from __future__ import annotations

import pytest

from metixel.backend import network_controller as nc
from metixel.backend.network_controller import NetworkController, NetworkState


@pytest.fixture
def controller() -> NetworkController:
    return NetworkController({})


class TestTestModeFlag:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(nc._TEST_MODE_ENV, raising=False)
        assert nc._test_mode_enabled() is False

    def test_enabled_when_set_to_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(nc._TEST_MODE_ENV, "1")
        assert nc._test_mode_enabled() is True

    def test_disabled_for_other_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(nc._TEST_MODE_ENV, "true")
        assert nc._test_mode_enabled() is False


class TestEthernetIgnoredInTestMode:
    def test_ethernet_ignored_when_test_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(nc._TEST_MODE_ENV, "1")
        ctrl = NetworkController({})
        # Even if the real ethernet check would report connected, test mode
        # forces it to False.
        monkeypatch.setattr(nc, "is_ethernet_connected", lambda: True)
        assert ctrl._is_ethernet_connected() is False

    def test_ethernet_checked_when_not_test_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(nc._TEST_MODE_ENV, raising=False)
        ctrl = NetworkController({})
        monkeypatch.setattr(nc, "is_ethernet_connected", lambda: True)
        assert ctrl._is_ethernet_connected() is True

    def test_any_connected_ignores_ethernet_in_test_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(nc._TEST_MODE_ENV, "1")
        ctrl = NetworkController({})
        monkeypatch.setattr(nc, "is_ethernet_connected", lambda: True)
        monkeypatch.setattr(nc, "is_connected", lambda: False)
        # Ethernet alone must NOT count as connected in test mode.
        assert ctrl._is_any_connected() is False

    def test_any_connected_uses_wifi_in_test_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(nc._TEST_MODE_ENV, "1")
        ctrl = NetworkController({})
        monkeypatch.setattr(nc, "is_ethernet_connected", lambda: True)
        monkeypatch.setattr(nc, "is_connected", lambda: True)
        assert ctrl._is_any_connected() is True


class TestStateMachineInTestMode:
    def test_disconnected_when_only_ethernet_in_test_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With test mode on and only ethernet up, the controller must NOT
        report CLIENT_CONNECTED — it should fall through to AP fallback."""
        monkeypatch.setenv(nc._TEST_MODE_ENV, "1")
        monkeypatch.setattr(nc, "is_wifi_hardware_present", lambda: True)
        monkeypatch.setattr(nc, "is_ethernet_connected", lambda: True)
        monkeypatch.setattr(nc, "is_connected", lambda: False)
        monkeypatch.setattr(nc, "has_saved_wifi_networks", lambda: False)
        monkeypatch.setattr(nc, "pre_scan_for_ap", lambda: None)
        monkeypatch.setattr(nc, "_start_ap", lambda: True)

        ctrl = NetworkController({})
        # First tick: CLIENT_CONNECTED → CLIENT_DISCONNECTED (ethernet ignored).
        state, _, _ = ctrl.tick()
        assert state == NetworkState.CLIENT_DISCONNECTED
        # Second tick: no saved networks → AP immediately.
        state, _, _ = ctrl.tick()
        assert state == NetworkState.AP_ACTIVE

    def test_connected_via_wifi_in_test_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(nc._TEST_MODE_ENV, "1")
        monkeypatch.setattr(nc, "is_wifi_hardware_present", lambda: True)
        monkeypatch.setattr(nc, "is_ethernet_connected", lambda: True)
        monkeypatch.setattr(nc, "is_connected", lambda: True)

        ctrl = NetworkController({})
        state, _, _ = ctrl.tick()
        assert state == NetworkState.CLIENT_CONNECTED
