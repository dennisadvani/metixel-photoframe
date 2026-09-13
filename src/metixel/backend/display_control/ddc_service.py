# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""DDC/CI service — capability caching + config-aware monitor control.

Wraps a :class:`~metixel.shared.ports.DdcController` and applies the
``ddc`` config section (enabled flag, display number).  Capability results
are cached until :meth:`refresh` or a successful ``set_vcp``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, cast

from metixel.shared.ddc_types import (
    DdcCapabilities,
    DdcFeature,
    DdcMonitor,
    DdcVcpValue,
    FeatureType,
    is_user_facing,
)
from metixel.shared.ports import DdcController

logger = logging.getLogger(__name__)


class DdcBusyError(RuntimeError):
    """Raised when another DDC probe is already running and cannot be awaited.

    A *transient* condition, not a configuration problem: the caller should
    retry rather than report the monitor as unsupported.
    """


class DdcService:
    """Backend-owned DDC/CI façade used by the web API."""

    def __init__(
        self,
        controller: DdcController,
        get_config: Callable[[], Mapping[str, Any]],
        *,
        cache_ttl_seconds: float = 60.0,
        probe_timeout_seconds: float = 10.0,
    ) -> None:
        self._controller = controller
        self._get_config = get_config
        self._cache_ttl = cache_ttl_seconds
        #: How long a *waiting* caller blocks for an in-flight probe before
        #: giving up with :class:`DdcBusyError`.  The owner is never interrupted
        #: — it always finishes and populates the cache.
        self._probe_timeout = probe_timeout_seconds
        self._lock = threading.Lock()
        #: Serialises the expensive ddcutil probes.  Held only while a probe is
        #: actually running (never during cheap cache lookups), so unrelated
        #: cache hits are not blocked behind I²C work.
        self._probe_lock = threading.Lock()
        self._monitors: list[DdcMonitor] | None = None
        self._monitors_at: float = 0.0
        self._caps: dict[int, tuple[float, DdcCapabilities]] = {}

    # -- Public API ----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return enablement + availability + detected monitors."""
        cfg = self._cfg()
        enabled = bool(cfg.get("enabled", False))
        if not enabled:
            return {
                "enabled": False,
                "available": False,
                "reason": "DDC/CI is disabled in settings",
                "display": int(cfg.get("display", 1) or 1),
                "monitors": [],
            }

        if not self._controller.available():
            return {
                "enabled": True,
                "available": False,
                "reason": "ddcutil is not installed or not on PATH",
                "display": int(cfg.get("display", 1) or 1),
                "monitors": [],
            }

        monitors = self._detect(force=False)
        if not monitors:
            return {
                "enabled": True,
                "available": False,
                "reason": (
                    "No DDC/CI-capable monitor detected. "
                    "Check the HDMI cable, I²C access, and that the display supports DDC."
                ),
                "display": int(cfg.get("display", 1) or 1),
                "monitors": [],
            }

        selected = self._resolve_display(monitors, cfg)
        return {
            "enabled": True,
            "available": True,
            "reason": None,
            "display": selected,
            "monitors": [m.to_dict() for m in monitors],
        }

    def capabilities(self, display: int | None = None) -> dict[str, Any]:
        """Return user-facing VCP features for the selected display."""
        status = self.status()
        if not status["available"]:
            return {
                **status,
                "model": "",
                "mccs_version": "",
                "features": [],
            }

        detected = self._detect(force=False)
        disp = display if display is not None else int(status["display"])
        if detected and not any(m.display == disp for m in detected):
            disp = detected[0].display

        caps = self._capabilities(disp, force=False)
        features = [f for f in caps.features if is_user_facing(f)]
        return {
            "enabled": True,
            "available": True,
            "reason": None,
            "display": disp,
            "model": caps.model,
            "mccs_version": caps.mccs_version,
            "features": [f.to_dict() for f in features],
            "monitors": status["monitors"],
        }

    def get_vcp(self, code: int, display: int | None = None) -> dict[str, Any]:
        status = self.status()
        if not status["available"]:
            raise DdcUnavailableError(status.get("reason") or "DDC/CI unavailable")

        disp = display if display is not None else int(status["display"])
        value = self._controller.get_vcp(disp, code)
        if value is None:
            raise DdcFeatureError(f"Unable to read VCP 0x{code:02X}")
        return cast(dict[str, Any], value.to_dict())

    def set_vcp(
        self,
        code: int,
        value: int,
        display: int | None = None,
    ) -> dict[str, Any]:
        status = self.status()
        if not status["available"]:
            raise DdcUnavailableError(status.get("reason") or "DDC/CI unavailable")

        disp = display if display is not None else int(status["display"])
        try:
            self._controller.set_vcp(disp, int(code), int(value))
        except Exception as exc:
            raise DdcFeatureError(str(exc) or f"Failed to set VCP 0x{code:02X}") from exc

        # Invalidate capability cache so the next read reflects the new value.
        with self._lock:
            self._caps.pop(disp, None)

        # Prefer a fresh readback; fall back to the written value.
        readback = self._controller.get_vcp(disp, code)
        if readback is not None:
            return cast(dict[str, Any], readback.to_dict())
        return cast(dict[str, Any], DdcVcpValue(code=code, current=int(value)).to_dict())

    def reset_factory(self, display: int | None = None) -> dict[str, Any]:
        """Restore the monitor to factory defaults (VCP 0x04)."""
        status = self.status()
        if not status["available"]:
            raise DdcUnavailableError(status.get("reason") or "DDC/CI unavailable")

        disp = display if display is not None else int(status["display"])
        try:
            self._controller.reset_factory(disp)
        except Exception as exc:
            raise DdcFeatureError(
                str(exc) or "Failed to reset monitor to factory defaults"
            ) from exc

        # Invalidate capability cache so the next read reflects the reset.
        with self._lock:
            self._caps.pop(disp, None)
        return {"status": "ok", "display": disp}

    def refresh(self) -> dict[str, Any]:
        """Invalidate caches and return a fresh status + capabilities.

        Raises :class:`DdcBusyError` if another probe is already in flight —
        the invalidation still takes effect, so the concurrent probe simply
        repopulates the cache with fresh data.
        """
        with self._lock:
            self._monitors = None
            self._monitors_at = 0.0
            self._caps.clear()
        status = self.status()
        if not status["available"]:
            return {**status, "model": "", "mccs_version": "", "features": []}
        return self.capabilities(display=int(status["display"]))

    # -- Internals -----------------------------------------------------------

    def _cfg(self) -> Mapping[str, Any]:
        try:
            return self._get_config() or {}
        except Exception:
            logger.debug("DDC config getter failed", exc_info=True)
            return {}

    def _resolve_display(
        self,
        monitors: list[DdcMonitor],
        cfg: Mapping[str, Any],
    ) -> int:
        preferred = int(cfg.get("display", 1) or 1)
        if any(m.display == preferred for m in monitors):
            return preferred
        return monitors[0].display

    def _detect(self, *, force: bool) -> list[DdcMonitor]:
        monitors = self._cached_monitors() if not force else None
        if monitors is not None:
            return monitors

        if not self._acquire_probe():
            # Another caller is already probing.  Its result is authoritative,
            # so reuse it rather than spawning a second competing ddcutil.
            cached = self._cached_monitors()
            if cached is not None:
                return cached
            with self._lock:
                known = list(self._monitors or [])
            if known:
                return known
            # Nothing cached yet: report a transient busy condition rather than
            # the misleading "no DDC/CI-capable monitor detected" reason.
            raise DdcBusyError("Another DDC/CI probe is already running")

        try:
            # Re-check: the previous owner may have refreshed the cache while
            # this caller was waiting for the lock.
            monitors = self._cached_monitors()
            if monitors is not None:
                return monitors
            try:
                monitors = list(self._controller.detect())
            except Exception:
                logger.warning("DDC detect failed", exc_info=True)
                monitors = []
            with self._lock:
                self._monitors = monitors
                self._monitors_at = time.monotonic()
            return list(monitors)
        finally:
            self._probe_lock.release()

    def _capabilities(self, display: int, *, force: bool) -> DdcCapabilities:
        caps = None if force else self._cached_capabilities(display)
        if caps is not None:
            return caps

        if not self._acquire_probe():
            cached = self._cached_capabilities(display)
            if cached is not None:
                return cached
            raise DdcBusyError("Another DDC/CI probe is already running")

        try:
            caps = None if force else self._cached_capabilities(display)
            if caps is not None:
                return caps
            try:
                caps = self._controller.capabilities(display)
            except Exception:
                logger.warning("DDC capabilities failed for display %s", display, exc_info=True)
                # Do NOT cache a transient failure — let the caller see the empty
                # result this call, but the next call retries the probe instead of
                # serving a poisoned empty feature list for the whole TTL (a busy
                # backend can make `ddcutil capabilities` exceed its 5s timeout,
                # e.g. right after the Immich download saturates the pipeline).
                caps = DdcCapabilities(display=display)
            if not isinstance(caps, DdcCapabilities):
                # Tolerate duck-typed fakes returning dicts / simple objects.
                caps = _coerce_capabilities(caps, display)
            with self._lock:
                # Only cache a non-empty result.  A monitor that genuinely reports
                # no features is indistinguishable from a transient failure at the
                # service level, but exposing empty features is a softer failure
                # than pinning the absence of brightness/contrast for 60s.
                if caps.features:
                    self._caps[display] = (time.monotonic(), caps)
            return caps
        finally:
            self._probe_lock.release()

    # -- Probe serialisation -------------------------------------------------

    def _acquire_probe(self) -> bool:
        """Take the probe lock, waiting briefly if another probe is in flight.

        Returns ``True`` when this caller now owns the probe, ``False`` when a
        different caller is already probing and the wait timed out.  The owner
        always releases in a ``finally``, so the lock can never leak.
        """
        try:
            return self._probe_lock.acquire(timeout=self._probe_timeout)
        except TypeError:  # pragma: no cover - only on non-timeout Lock impls
            return self._probe_lock.acquire()

    def _cached_monitors(self) -> list[DdcMonitor] | None:
        """Return the cached monitor list while it is still within the TTL."""
        now = time.monotonic()
        with self._lock:
            if self._monitors is not None and (now - self._monitors_at) < self._cache_ttl:
                return list(self._monitors)
        return None

    def _cached_capabilities(self, display: int) -> DdcCapabilities | None:
        """Return cached capabilities for *display* if still within the TTL."""
        now = time.monotonic()
        with self._lock:
            cached = self._caps.get(display)
            # Copy under the lock — DdcCapabilities is mutable and the cache
            # entry may be replaced by another thread immediately afterwards.
            if cached is not None and (now - cached[0]) < self._cache_ttl:
                return cached[1]
        return None


def _coerce_capabilities(raw: Any, display: int) -> DdcCapabilities:
    """Best-effort conversion for fakes that return dict-shaped capabilities."""
    if isinstance(raw, DdcCapabilities):
        return raw
    if not isinstance(raw, dict):
        return DdcCapabilities(display=display)
    features: list[DdcFeature] = []
    for item in raw.get("features") or []:
        if isinstance(item, DdcFeature):
            features.append(item)
            continue
        if not isinstance(item, dict):
            continue
        features.append(
            DdcFeature(
                code=int(item.get("code", 0)),
                name=str(item.get("name", "")),
                feature_type=_as_feature_type(item.get("type") or item.get("feature_type")),
                current=item.get("current"),
                maximum=item.get("maximum") or item.get("max"),
                readable=bool(item.get("readable", True)),
                writable=bool(item.get("writable", True)),
                icon=str(item.get("icon", "")),
            )
        )
    return DdcCapabilities(
        display=int(raw.get("display", display)),
        model=str(raw.get("model", "")),
        mccs_version=str(raw.get("mccs_version", "")),
        features=features,
    )


def _as_feature_type(value: Any) -> FeatureType:
    if value in ("continuous", "discrete", "table", "unknown"):
        return cast(FeatureType, value)
    return "unknown"


class DdcUnavailableError(RuntimeError):
    """Raised when DDC is disabled or no capable monitor is present."""


class DdcFeatureError(RuntimeError):
    """Raised when a get/set VCP operation fails."""
