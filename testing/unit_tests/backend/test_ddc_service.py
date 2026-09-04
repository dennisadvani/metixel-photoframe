# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""DdcService — capability caching and enable/disable gating."""

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
    """In-memory DdcController for unit tests."""

    def __init__(
        self,
        *,
        available: bool = True,
        monitors: list[DdcMonitor] | None = None,
        features: list[DdcFeature] | None = None,
    ) -> None:
        self._available = available
        self.monitors = monitors or [
            DdcMonitor(display=1, model="Test Monitor", mfg="TST"),
        ]
        self.features = features or [
            DdcFeature(
                code=0x10,
                name="Brightness",
                feature_type="continuous",
                current=50,
                maximum=100,
                writable=True,
            ),
            DdcFeature(
                code=0x14,
                name="Colour Preset",
                feature_type="discrete",
                current=0x05,
                options=[
                    DdcDiscreteOption(0x01, "sRGB"),
                    DdcDiscreteOption(0x05, "6500 K"),
                ],
                writable=True,
            ),
            DdcFeature(
                code=0x02,
                name="New control value",
                feature_type="discrete",
                current=0x01,
                writable=False,
            ),
        ]
        self.set_calls: list[tuple[int, int, int]] = []
        self.reset_calls: list[int] = []

    def available(self) -> bool:
        return self._available

    def detect(self) -> list[DdcMonitor]:
        return list(self.monitors)

    def capabilities(self, display: int) -> DdcCapabilities:
        return DdcCapabilities(
            display=display,
            model=self.monitors[0].model if self.monitors else "",
            features=list(self.features),
        )

    def get_vcp(self, display: int, code: int) -> DdcVcpValue | None:
        for feat in self.features:
            if feat.code == code and feat.current is not None:
                return DdcVcpValue(
                    code=code,
                    current=feat.current,
                    maximum=feat.maximum or 0,
                    name=feat.name,
                    feature_type=feat.feature_type,
                )
        return None

    def set_vcp(self, display: int, code: int, value: int) -> None:
        self.set_calls.append((display, code, value))
        for feat in self.features:
            if feat.code == code:
                feat.current = value
                return

    def reset_factory(self, display: int) -> None:
        self.reset_calls.append(display)


class TestDdcService:
    def test_disabled_returns_unavailable(self) -> None:
        svc = DdcService(FakeDdcController(), get_config=lambda: {"enabled": False})
        status = svc.status()
        assert status["enabled"] is False
        assert status["available"] is False
        assert "disabled" in (status["reason"] or "").lower()

    def test_missing_binary(self) -> None:
        svc = DdcService(
            FakeDdcController(available=False),
            get_config=lambda: {"enabled": True, "display": 1},
        )
        status = svc.status()
        assert status["available"] is False
        assert "ddcutil" in (status["reason"] or "").lower()

    def test_capabilities_filters_hidden(self) -> None:
        svc = DdcService(
            FakeDdcController(),
            get_config=lambda: {"enabled": True, "display": 1},
        )
        caps = svc.capabilities()
        assert caps["available"] is True
        codes = {f["code"] for f in caps["features"]}
        assert 0x10 in codes
        assert 0x14 in codes
        assert 0x02 not in codes  # New Control Value is hidden

    def test_set_vcp_updates(self) -> None:
        fake = FakeDdcController()
        svc = DdcService(fake, get_config=lambda: {"enabled": True, "display": 1})
        result = svc.set_vcp(0x10, 80)
        assert result["current"] == 80
        assert fake.set_calls == [(1, 0x10, 80)]

    def test_reset_factory(self) -> None:
        fake = FakeDdcController()
        svc = DdcService(fake, get_config=lambda: {"enabled": True, "display": 1})
        result = svc.reset_factory()
        assert result["status"] == "ok"
        assert fake.reset_calls == [1]

    def test_reset_factory_unavailable(self) -> None:
        svc = DdcService(
            FakeDdcController(available=False),
            get_config=lambda: {"enabled": True, "display": 1},
        )
        from metixel.backend.display_control.ddc_service import DdcUnavailableError

        with pytest.raises(DdcUnavailableError):
            svc.reset_factory()

    def test_isinstance_protocol(self) -> None:
        from metixel.shared.ports import DdcController

        assert isinstance(FakeDdcController(), DdcController)
