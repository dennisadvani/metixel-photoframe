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


class DdcService:
    """Backend-owned DDC/CI façade used by the web API."""

    def __init__(
        self,
        controller: DdcController,
        get_config: Callable[[], Mapping[str, Any]],
        *,
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        self._controller = controller
        self._get_config = get_config
        self._cache_ttl = cache_ttl_seconds
        self._lock = threading.Lock()
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
        """Invalidate caches and return a fresh status + capabilities."""
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
        now = time.monotonic()
        with self._lock:
            if (
                not force
                and self._monitors is not None
                and (now - self._monitors_at) < self._cache_ttl
            ):
                return list(self._monitors)
        try:
            monitors = list(self._controller.detect())
        except Exception:
            logger.warning("DDC detect failed", exc_info=True)
            monitors = []
        with self._lock:
            self._monitors = monitors
            self._monitors_at = time.monotonic()
        return list(monitors)

    def _capabilities(self, display: int, *, force: bool) -> DdcCapabilities:
        now = time.monotonic()
        with self._lock:
            cached = self._caps.get(display)
            if not force and cached is not None and (now - cached[0]) < self._cache_ttl:
                return cached[1]
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
