# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Web API tests for /api/ddc/*."""

from __future__ import annotations

import pytest

from metixel.backend.display_control.ddc_service import DdcService
from metixel.shared.ddc_types import (
    DdcCapabilities,
    DdcDiscreteOption,
    DdcFeature,
    DdcMonitor,
    DdcVcpValue,
)


class FakeDdcController:
    def __init__(self) -> None:
        self.values = {0x10: 50, 0x14: 0x05}

    def available(self) -> bool:
        return True

    def detect(self) -> list[DdcMonitor]:
        return [DdcMonitor(display=1, model="FakePanel")]

    def capabilities(self, display: int) -> DdcCapabilities:
        return DdcCapabilities(
            display=display,
            model="FakePanel",
            features=[
                DdcFeature(
                    code=0x10,
                    name="Brightness",
                    feature_type="continuous",
                    current=self.values[0x10],
                    maximum=100,
                ),
                DdcFeature(
                    code=0x14,
                    name="Colour Preset",
                    feature_type="discrete",
                    current=self.values[0x14],
                    options=[
                        DdcDiscreteOption(0x01, "sRGB"),
                        DdcDiscreteOption(0x05, "6500 K"),
                    ],
                ),
            ],
        )

    def get_vcp(self, display: int, code: int) -> DdcVcpValue | None:
        if code not in self.values:
            return None
        return DdcVcpValue(
            code=code,
            current=self.values[code],
            maximum=100 if code == 0x10 else 0,
            feature_type="continuous" if code == 0x10 else "discrete",
        )

    def set_vcp(self, display: int, code: int, value: int) -> None:
        self.values[code] = value

    def reset_factory(self, display: int) -> None:
        self.values = {0x10: 50, 0x14: 0x05}


@pytest.fixture
def ddc_app(app, mock_state):
    """Flask app with an injected DdcService (enabled)."""
    mock_state.update_config("ddc", {"enabled": True, "display": 1})
    svc = DdcService(
        FakeDdcController(),
        get_config=lambda: mock_state.config.ddc,
    )
    app.config["METIXEL_DDC"] = svc
    return app


@pytest.fixture
def ddc_client(ddc_app):
    return ddc_app.test_client()


class TestDdcRoutes:
    def test_status_disabled(self, client, mock_state) -> None:
        mock_state.update_config("ddc", {"enabled": False})
        from metixel.backend.display_control.ddc_service import DdcService

        client.application.config["METIXEL_DDC"] = DdcService(
            FakeDdcController(),
            get_config=lambda: mock_state.config.ddc,
        )
        resp = client.get("/api/ddc/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["enabled"] is False
        assert data["available"] is False

    def test_status_available(self, ddc_client) -> None:
        resp = ddc_client.get("/api/ddc/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["available"] is True
        assert data["monitors"][0]["model"] == "FakePanel"

    def test_capabilities(self, ddc_client) -> None:
        resp = ddc_client.get("/api/ddc/capabilities")
        assert resp.status_code == 200
        data = resp.get_json()
        codes = {f["code"] for f in data["features"]}
        assert 0x10 in codes
        assert 0x14 in codes

    def test_get_and_set_vcp(self, ddc_client) -> None:
        resp = ddc_client.get("/api/ddc/vcp/10")
        assert resp.status_code == 200
        assert resp.get_json()["current"] == 50

        resp = ddc_client.put("/api/ddc/vcp/0x10", json={"value": 80})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "ok"
        assert body["current"] == 80

    def test_set_missing_value(self, ddc_client) -> None:
        resp = ddc_client.put("/api/ddc/vcp/10", json={})
        assert resp.status_code == 400

    def test_code_is_parsed_as_hex(self, ddc_client) -> None:
        """The VCP code in the URL is hex — 0x10 is brightness, not 0x16."""
        # 0x10 (brightness) — the frontend sends the hex form.
        resp = ddc_client.put("/api/ddc/vcp/0x10", json={"value": 80})
        assert resp.status_code == 200
        assert resp.get_json()["current"] == 80
        # Decimal "16" is parsed as hex 0x16 (Red Gain) — a DIFFERENT feature
        # than brightness (0x10).  The fake controller stores it separately,
        # so brightness must remain unchanged.
        resp = ddc_client.put("/api/ddc/vcp/16", json={"value": 5})
        assert resp.status_code == 200
        # Brightness (0x10) is still 80 — the decimal "16" hit Red Gain (0x16).
        resp = ddc_client.get("/api/ddc/vcp/0x10")
        assert resp.get_json()["current"] == 80

    def test_refresh(self, ddc_client) -> None:
        resp = ddc_client.post("/api/ddc/refresh")
        assert resp.status_code == 200
        assert resp.get_json()["available"] is True

    def test_reset_factory(self, ddc_client) -> None:
        resp = ddc_client.post("/api/ddc/reset", json={})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_service_missing(self, client) -> None:
        client.application.config.pop("METIXEL_DDC", None)
        resp = client.get("/api/ddc/status")
        assert resp.status_code == 503
