# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the MQTTClient.status() connection-state reporting."""

from __future__ import annotations

from pathlib import Path


class TestMQTTClientStatus:
    def _make(self, tmp_path: Path):
        from metixel.backend.mqtt_client import MQTTClient
        from metixel.backend.state import StateManager
        from metixel.shared.config import Config

        config_path = tmp_path / "config.json"
        Config().save(config_path)
        state = StateManager(config_path, tmp_path / "run")
        return MQTTClient(state, None)

    def test_status_disabled(self, tmp_path: Path) -> None:
        client = self._make(tmp_path)
        body = client.status()
        assert body["status"] == "disabled"
        assert body["enabled"] is False

    def test_status_connected(self, tmp_path: Path) -> None:
        client = self._make(tmp_path)
        client._state.update_config("mqtt", {"enabled": True})
        client._status = "connected"
        client._connected = True
        client._broker_host = "192.168.1.10"
        client._broker_port = 1883
        body = client.status()
        assert body["status"] == "connected"
        assert body["broker"] == "192.168.1.10"

    def test_status_auth_error(self, tmp_path: Path) -> None:
        client = self._make(tmp_path)
        client._state.update_config("mqtt", {"enabled": True})
        client._status = "rejected"
        client._connected = False
        client._last_error = "Not authorized"
        body = client.status()
        assert body["status"] == "auth_error"
        assert body["error"] == "Not authorized"

    def test_status_connecting(self, tmp_path: Path, monkeypatch) -> None:
        import time as time_mod

        client = self._make(tmp_path)
        client._state.update_config("mqtt", {"enabled": True})
        client._status = "connecting"
        monkeypatch.setattr(time_mod, "monotonic", lambda: 100.0)
        client._connecting_since = 99.0  # 1s ago → still connecting
        body = client.status()
        assert body["status"] == "connecting"

    def test_status_not_responding_when_stuck(self, tmp_path: Path, monkeypatch) -> None:
        import time as time_mod

        client = self._make(tmp_path)
        client._state.update_config("mqtt", {"enabled": True})
        client._status = "connecting"
        client._connecting_since = 5.0
        monkeypatch.setattr(time_mod, "monotonic", lambda: 25.0)  # 20s elapsed
        body = client.status()
        assert body["status"] == "not_responding"
