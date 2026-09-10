# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""DdcService — capability caching and enable/disable gating."""

from __future__ import annotations

import threading
import time

import pytest

from metixel.backend.display_control.ddc_service import DdcBusyError, DdcService
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


class _RecoveringController(FakeDdcController):
    """FakeController that returns empty capabilities N times, then recovers."""

    def __init__(self, *, fail_times: int = 1) -> None:
        super().__init__()
        self._remaining_failures = fail_times
        self._calls = 0

    def capabilities(self, display: int) -> DdcCapabilities:
        self._calls += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            # Simulate the production controller raising on a busy backend.
            return DdcCapabilities(display=display)
        return super().capabilities(display)


class TestTransientCapabilityFailure:
    def test_empty_capabilities_not_cached_and_retried(self) -> None:
        """A transient empty probe must not be cached; the next call retries."""
        fake = _RecoveringController(fail_times=1)
        # Small TTL so a stale poisoned cache would otherwise mask the retry.
        svc = DdcService(
            fake,
            get_config=lambda: {"enabled": True, "display": 1},
            cache_ttl_seconds=600.0,
        )

        # First call: probe fails → empty features, NOT cached.
        caps = svc.capabilities()
        assert caps["available"] is True
        assert caps["features"] == []

        # Second call: must re-probe (transient failure wasn't cached) and
        # now see the real monitor's brightness feature.
        caps2 = svc.capabilities()
        codes = {f["code"] for f in caps2["features"]}
        assert 0x10 in codes
        assert fake._calls == 2, "expected the second call to re-run the probe"


class _SlowCountingController(FakeDdcController):
    """Controller that blocks inside detect()/capabilities() and counts calls.

    Reproduces the production stampede: several concurrent API requests all
    miss a cold cache and each spawn their own long-running ddcutil probe.
    """

    def __init__(self, *, hold: float = 0.35) -> None:
        super().__init__()
        self._hold = hold
        self.detect_calls = 0
        self.capabilities_calls = 0
        self._entered = threading.Event()

    def detect(self) -> list[DdcMonitor]:
        self.detect_calls += 1
        self._entered.set()
        time.sleep(self._hold)
        return super().detect()

    def capabilities(self, display: int) -> DdcCapabilities:
        self.capabilities_calls += 1
        time.sleep(self._hold)
        return super().capabilities(display)


class TestProbeSerialisation:
    """Only one ddcutil probe may run at a time (no I²C cache stampede)."""

    def test_concurrent_status_probes_do_not_stampede(self) -> None:
        """N concurrent status() calls must trigger a single detect() probe.

        This is the regression guard for the observed 147-process ddcutil burst
        plus `flock() for /dev/i2c-2 failed` errors right after a restart.
        """
        fake = _SlowCountingController(hold=0.4)
        svc = DdcService(
            fake,
            get_config=lambda: {"enabled": True, "display": 1},
            probe_timeout_seconds=5.0,
        )

        results: list[dict] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(6)

        def worker() -> None:
            barrier.wait()  # release all threads simultaneously
            try:
                results.append(svc.status())
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"probes raised instead of sharing a result: {errors}"
        assert len(results) == 6
        # The core assertion: one probe, not six.
        assert fake.detect_calls == 1, (
            f"expected a single serialised detect probe, got {fake.detect_calls}"
        )
        # Every caller still gets a usable answer.
        assert all(r["available"] is True for r in results)

    def test_capabilities_raise_busy_when_probe_in_flight(self) -> None:
        """A caller that cannot wait is told to retry, not that DDC is absent."""
        fake = _SlowCountingController(hold=0.5)
        svc = DdcService(
            fake,
            get_config=lambda: {"enabled": True, "display": 1},
            probe_timeout_seconds=0.05,  # give up almost immediately
        )

        probe_started = threading.Event()
        original_detect = fake.detect

        def signalling_detect() -> list[DdcMonitor]:
            probe_started.set()
            return original_detect()

        fake.detect = signalling_detect  # type: ignore[method-assign]

        holder: list[object] = []

        def hold_probe() -> None:
            # status() → _detect() acquires the probe lock and blocks in detect().
            holder.append(svc.status())

        t = threading.Thread(target=hold_probe)
        t.start()
        assert probe_started.wait(timeout=5), "probe never started"
        try:
            with pytest.raises(DdcBusyError):
                svc.capabilities()
        finally:
            t.join(timeout=10)

        assert holder, "the owner probe should have completed"
        # The owner was not interrupted; it populated the cache.
        assert fake.detect_calls == 1

    def test_busy_error_is_not_an_unavailable_monitor(self) -> None:
        """DdcBusyError must be distinguishable from 'no monitor detected'."""
        fake = _SlowCountingController(hold=0.5)
        svc = DdcService(
            fake,
            get_config=lambda: {"enabled": True, "display": 1},
            probe_timeout_seconds=0.05,
        )
        started = threading.Event()
        original = fake.detect

        def signalling_detect() -> list[DdcMonitor]:
            started.set()
            return original()

        fake.detect = signalling_detect  # type: ignore[method-assign]
        t = threading.Thread(target=lambda: svc.status())
        t.start()
        assert started.wait(timeout=5)
        try:
            with pytest.raises(DdcBusyError) as exc_info:
                svc.status()
            assert "another ddc" in str(exc_info.value).lower()
        finally:
            t.join(timeout=10)
