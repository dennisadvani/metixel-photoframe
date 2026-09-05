# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Network API endpoints — with ``network_manager`` functions mocked."""

from __future__ import annotations

import json
import time
from unittest import mock


def _wait_for_call(callable_mock, timeout: float = 3.0) -> None:
    """Busy-wait until a background thread has invoked the mock."""
    deadline = time.time() + timeout
    while time.time() < deadline and callable_mock.call_count == 0:
        time.sleep(0.01)


class TestNetworkStatus:
    def test_status(self, client, monkeypatch):
        import metixel.backend.web.routes.network as net_mod

        monkeypatch.setattr(
            net_mod,
            "get_connection_status",
            lambda: {"ip": "10.0.0.5", "interface_type": "wifi"},
        )
        monkeypatch.setattr(net_mod, "is_ap_mode_active", lambda: False)
        monkeypatch.setattr(net_mod, "is_connected", lambda: True)

        resp = client.get("/api/network/status")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ip"] == "10.0.0.5"
        assert data["ap_mode_active"] is False


class TestNetworkScan:
    def test_scan(self, client, monkeypatch):
        import metixel.backend.web.routes.network as net_mod

        monkeypatch.setattr(net_mod, "scan_networks", lambda: [{"ssid": "Net1"}])
        monkeypatch.setattr(net_mod, "is_ap_mode_active", lambda: False)

        resp = client.get("/api/network/scan")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["networks"] == [{"ssid": "Net1"}]
        assert data["cached"] is False


class TestNetworkConnect:
    def test_requires_ssid(self, client):
        resp = client.post("/api/network/connect", json={})
        assert resp.status_code == 400

    def test_connect(self, client, monkeypatch):
        import metixel.backend.web.routes.network as net_mod

        fake = mock.MagicMock(return_value=(False, "could not connect"))
        monkeypatch.setattr(net_mod, "connect_to_network", fake)

        resp = client.post("/api/network/connect", json={"ssid": "MyWiFi", "password": "secret"})
        assert resp.status_code == 200
        assert json.loads(resp.data)["status"] == "ok"
        _wait_for_call(fake)
        fake.assert_called_once_with("MyWiFi", "secret")


class TestNetworkForget:
    def test_requires_ssid(self, client):
        resp = client.post("/api/network/forget", json={})
        assert resp.status_code == 400

    def test_forget(self, client, monkeypatch):
        import metixel.backend.web.routes.network as net_mod

        monkeypatch.setattr(net_mod, "forget_network", lambda ssid: True)
        resp = client.post("/api/network/forget", json={"ssid": "MyWiFi"})
        assert resp.status_code == 200
        assert json.loads(resp.data)["status"] == "ok"


class TestApStatus:
    def test_ap_active(self, client, monkeypatch):
        import metixel.backend.web.routes.network as net_mod

        monkeypatch.setattr(net_mod, "is_ap_mode_active", lambda: True)
        monkeypatch.setattr(net_mod, "is_connected", lambda: False)

        resp = client.get("/api/network/ap-status")
        assert resp.status_code == 200
        assert json.loads(resp.data) == {"active": True}
