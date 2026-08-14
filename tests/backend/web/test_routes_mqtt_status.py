# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the MQTT broker status route (``GET /api/system/mqtt-status``)."""

from __future__ import annotations


class _FakeMqttClient:
    """Minimal stand-in exposing the daemon's MQTT client status()."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def status(self) -> dict:
        return dict(self._payload)


class _FakeDaemon:
    def __init__(self, mqtt_client) -> None:
        self._mqtt_client = mqtt_client


class TestMQTTStatusRoute:
    def test_returns_disabled_when_no_client(self, app, client, mock_state) -> None:
        """Without a daemon/MQTT client, falls back to the config state."""
        mock_state.update_config("mqtt", {"enabled": False})
        resp = client.get("/api/system/mqtt-status")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["enabled"] is False
        assert body["status"] == "disabled"

    def test_returns_client_status_when_present(self, app, client) -> None:
        """When the daemon exposes an MQTT client, its status is returned."""
        app.config["METIXEL_DAEMON"] = _FakeDaemon(
            _FakeMqttClient(
                {"enabled": True, "status": "connected", "broker": "10.0.0.1", "port": 1883}
            )
        )

        resp = client.get("/api/system/mqtt-status")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "connected"
        assert body["broker"] == "10.0.0.1"
