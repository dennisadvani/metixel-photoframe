# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Platform hardware adapters for the display backend.

Extracts three platform-specific concerns out of ``dispmanx_backend.py``
so they are independently testable and their mutable state is thread-safe:

* :class:`GpuInfo` — GPU memory introspection (``vcgencmd`` + DRM debugfs)
  with a TTL cache, plus ``glFinish`` for DMA-safe texture uploads.
* :class:`WlrOutput` — wlr-randr output enumeration, auto-detection of the
  real monitor, and phantom-output cleanup, with a lock around the cached
  output name.
* :class:`DisplayPower` — the three-tier display-power fallback chain
  (wlr-randr → DRM DPMS sysfs → ``vcgencmd``).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import subprocess
import threading
import time
from typing import Any

from metixel.shared.platform import is_raspberry_pi, read_vcgencmd_mem, read_vcgencmd_mem_str

logger = logging.getLogger(__name__)

#: Path to the wlr-randr binary (wlroots/Wayland compositors).
_WLR_BIN = "/usr/bin/wlr-randr"

#: Minimal environment for wlr-randr subprocesses — avoids DISPLAY=:0
#: (XWayland) conflicts and missing runtime-dir/HOME vars.
_WLR_ENV: dict[str, str] = {
    "WAYLAND_DISPLAY": "wayland-0",
    "XDG_RUNTIME_DIR": "/run/user/1000",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/home/pi"),
}

#: Map a clockwise rotation in degrees to the wlr-randr ``--transform`` value.
_WLR_TRANSFORMS: dict[int, str] = {
    0: "normal",
    90: "90",
    180: "180",
    270: "270",
}


def _wlr_transform(rotation: int) -> str:
    """Return the wlr-randr ``--transform`` value for a clockwise rotation.

    Unsupported values fall back to ``normal`` (0°).
    """
    return _WLR_TRANSFORMS.get(rotation, "normal")


class GpuInfo:
    """GPU memory introspection with a short-TTL cache.

    Reading ``vcgencmd`` spawns a subprocess and the DRM debugfs reads are
    sysfs I/O — neither should happen at frame rate, so results are cached
    for a few seconds.  All cache access is guarded by a lock.
    """

    _GPU_MEM_CACHE_TTL: float = 5.0  # seconds

    def __init__(self) -> None:
        self._cache_lock = threading.Lock()
        self._gpu_mem_cache: dict[str, Any] | None = None
        self._gpu_mem_cache_time: float = 0.0

    def snapshot(self, texture_count: int, max_textures: int) -> dict[str, Any] | None:
        """Read GPU memory usage from ``vcgencmd`` and DRM debugfs.

        Args:
            texture_count: Current number of GPU-resident textures.
            max_textures: Configured texture cap.

        Returns a dict with ``gpu_total_mb``, ``reloc_used_mb``,
        ``malloc_used_mb``, ``v3d_bo_count``, ``v3d_bo_kb``,
        ``texture_count``, or ``None`` if the tools are unavailable.
        """
        now = time.monotonic()
        with self._cache_lock:
            if (
                self._gpu_mem_cache is not None
                and (now - self._gpu_mem_cache_time) < self._GPU_MEM_CACHE_TTL
            ):
                return self._gpu_mem_cache

        info: dict[str, Any] = {
            "texture_count": texture_count,
            "max_textures": max_textures,
        }

        gpu_total_mb = read_vcgencmd_mem("gpu")
        if gpu_total_mb is not None:
            info["gpu_total_mb"] = gpu_total_mb
        reloc_mb = read_vcgencmd_mem("reloc")
        if reloc_mb is not None:
            info["reloc_used_mb"] = reloc_mb
        malloc_mb = read_vcgencmd_mem("malloc")
        if malloc_mb is not None:
            info["malloc_used_mb"] = malloc_mb

        try:
            # /sys/kernel/debug/dri/0/bo_stats — "V3D:  107292kb BOs (34)"
            with open("/sys/kernel/debug/dri/0/bo_stats") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith("V3D:") and "kb BOs" in stripped:
                        parts = stripped.split()
                        if len(parts) >= 4:
                            kb_str = parts[1].rstrip("kb")
                            count_str = parts[3].lstrip("(").rstrip(")")
                            info["v3d_bo_kb"] = int(kb_str)
                            info["v3d_bo_count"] = int(count_str)
                        break
        except (OSError, ValueError, IndexError):
            pass

        with self._cache_lock:
            self._gpu_mem_cache = info
            self._gpu_mem_cache_time = now
        return info

    @staticmethod
    def flush() -> None:
        """Block until the GPU command queue drains (``glFinish``).

        On VideoCore IV (Pi 2/3), pi3d's ``free_after_load=True`` can
        release the CPU numpy array before the DMA upload completes,
        causing black textures.  Uses ctypes to call ``glFinish`` from the
        system GLESv2 library — no PyOpenGL dependency.
        """
        try:
            from ctypes import cdll, util

            lib_name = util.find_library("GLESv2")
            if lib_name is None:
                logger.debug("flush_gpu: GLESv2 library not found — skipping")
                return
            gl = cdll.LoadLibrary(lib_name)
            gl.glFinish()
        except Exception:
            pass  # Non-Pi or GL not available

    @staticmethod
    def gpu_mem_str() -> str:
        """GPU allocation string from ``vcgencmd`` (or ``"unknown"``)."""
        return read_vcgencmd_mem_str("gpu", fallback="unknown")

    @staticmethod
    def drm_driver() -> str:
        """Detect the active DRM/KMS driver (vc4, vc4-fkms-v3d, …)."""
        try:
            for entry in os.listdir("/sys/class/drm"):
                if entry.startswith("card"):
                    card_path = f"/sys/class/drm/{entry}/device/driver"
                    if os.path.islink(card_path):
                        return os.path.basename(os.readlink(card_path))
            return "none"
        except Exception:
            return "unknown"


class WlrOutput:
    """wlr-randr output enumeration, auto-detection, and control.

    A Raspberry Pi exposes two HDMI connectors; only one usually has a
    real monitor.  This resolves the correct output (env override →
    auto-detected real monitor → ``HDMI-A-1``) and drives power changes
    via ``wlr-randr``.  The cached output name is guarded by a lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wlr_output: str | None = None

    @staticmethod
    def override() -> str:
        """Explicit output override from ``METIXEL_WLR_OUTPUT`` (empty if unset)."""
        return os.environ.get("METIXEL_WLR_OUTPUT", "").strip()

    def _get_cached(self) -> str | None:
        with self._lock:
            return self._wlr_output

    def _set_cached(self, value: str | None) -> None:
        with self._lock:
            self._wlr_output = value

    def resolve(self, fallback: bool = True) -> str:
        """Return the output name to target with wlr-randr.

        Priority: env override → cached/auto-detected real monitor →
        ``HDMI-A-1`` (when ``fallback``).
        """
        override = self.override()
        if override:
            return override
        cached = self._get_cached()
        if cached:
            return cached
        detected = self.detect()
        if detected:
            self._set_cached(detected)
            return detected
        if fallback:
            return self.resolve(fallback=False)
        return "HDMI-A-1"

    @staticmethod
    def detect() -> str | None:
        """Auto-detect the Wayland output with a real monitor attached.

        Picks an output with EDID-derived make/model, else one advertising
        a ``preferred`` mode, else the enabled output with the highest
        resolution.  Returns ``None`` if wlr-randr is unavailable or fails.
        """
        if not os.path.exists(_WLR_BIN):
            logger.debug("wlr-randr not installed — cannot detect display output")
            return None
        try:
            result = subprocess.run(
                [_WLR_BIN, "--json"],
                capture_output=True,
                timeout=5,
                env=_WLR_ENV,
            )
            if result.returncode != 0:
                logger.warning(
                    "wlr-randr --json failed (%d): %s",
                    result.returncode,
                    result.stderr.decode(errors="replace").strip(),
                )
                return None
            outputs = json.loads(result.stdout.decode(errors="replace") or "[]")
        except Exception:
            logger.warning("Failed to detect display output via wlr-randr", exc_info=True)
            return None

        if not isinstance(outputs, list) or not outputs:
            return None

        enabled = [o for o in outputs if o.get("enabled")]
        if not enabled:
            enabled = outputs

        def _score(out: dict) -> float:
            score = 0.0
            if out.get("make") or out.get("model"):
                score += 100.0
            for mode in out.get("modes", []) or []:
                if mode.get("preferred"):
                    score += 10.0
                if mode.get("current"):
                    score += 1.0
                    score += (mode.get("width", 0) * mode.get("height", 0)) / 1_000_000.0
            return score

        best = max(enabled, key=_score)
        name = best.get("name")
        if isinstance(name, str):
            logger.info(
                "Detected display output: %s (make=%s model=%s)",
                name,
                best.get("make"),
                best.get("model"),
            )
            return name
        return None

    def list_modes(self) -> list[dict[str, Any]]:
        """Return the real monitor's supported modes from wlr-randr.

        Each entry is ``{"width", "height", "refresh", "preferred", "current"}``
        for the resolved output.  Returns an empty list if wlr-randr is
        unavailable or the output cannot be resolved.
        """
        if not os.path.exists(_WLR_BIN):
            logger.debug("wlr-randr not installed — cannot list display modes")
            return []
        try:
            result = subprocess.run(
                [_WLR_BIN, "--json"],
                capture_output=True,
                timeout=5,
                env=_WLR_ENV,
            )
            if result.returncode != 0:
                logger.warning(
                    "wlr-randr --json failed (%d): %s",
                    result.returncode,
                    result.stderr.decode(errors="replace").strip(),
                )
                return []
            outputs = json.loads(result.stdout.decode(errors="replace") or "[]")
        except Exception:
            logger.warning("Failed to list display modes via wlr-randr", exc_info=True)
            return []

        if not isinstance(outputs, list) or not outputs:
            return []

        target = self.resolve(fallback=False)
        for out in outputs:
            if out.get("name") == target:
                modes = out.get("modes", []) or []
                return [m for m in modes if isinstance(m, dict)]
        return []

    def set_mode(
        self,
        *,
        width: int = 0,
        height: int = 0,
        refresh_rate: int = 0,
        rotation: int = 0,
    ) -> bool:
        """Apply a resolution / refresh rate / rotation via wlr-randr.

        Only the non-zero / non-default arguments are applied (0 width,
        height, or refresh rate means "leave unchanged"; rotation 0 means
        "no rotation").  Returns True on success, False if wlr-randr is
        unavailable or the mode is unsupported (caller should fall back to
        the native mode and log a warning).

        wlr-randr expresses refresh rate as part of the mode string:
        ``--mode <width>x<height>[@<refresh>Hz]`` — there is no separate
        ``--rate`` flag.
        """
        try:
            if not os.path.exists(_WLR_BIN):
                logger.debug("wlr-randr not installed at %s", _WLR_BIN)
                return False

            output = self.resolve()
            cmd = [_WLR_BIN, "--output", output]
            if width > 0 and height > 0:
                mode = f"{width}x{height}"
                if refresh_rate > 0:
                    mode += f"@{refresh_rate}Hz"
                cmd += ["--mode", mode]
            # A refresh rate without an explicit resolution cannot be
            # expressed in wlr-randr's mode string — skip it (the caller
            # normally supplies width/height together with the rate).
            if rotation:
                cmd += ["--transform", _wlr_transform(rotation)]

            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=5,
                env=_WLR_ENV,
            )
            if result.returncode == 0:
                logger.info(
                    "wlr-randr set mode on %s: %dx%d @ %dHz rot=%d",
                    output,
                    width,
                    height,
                    refresh_rate,
                    rotation,
                )
                return True

            stderr = result.stderr.decode(errors="replace").strip()
            logger.warning("wlr-randr set_mode exited %d: %s", result.returncode, stderr)
            return False
        except FileNotFoundError:
            logger.debug("wlr-randr not installed — cannot set display mode")
            return False
        except Exception:
            logger.warning("wlr-randr set_mode failed", exc_info=True)
            return False

    def set_power(self, on: bool) -> bool:
        """Toggle the display via wlr-randr. Returns True on success.

        Targets the resolved output.  If the cached output becomes stale
        (monitor moved to another port), re-detects once and retries.
        """
        try:
            if not os.path.exists(_WLR_BIN):
                logger.debug("wlr-randr not installed at %s", _WLR_BIN)
                return False

            output = self.resolve()
            cmd = [_WLR_BIN, "--output", output, "--on" if on else "--off"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=5,
                env=_WLR_ENV,
            )
            if result.returncode == 0:
                return True

            stderr = result.stderr.decode(errors="replace").strip()
            logger.warning("wlr-randr exited %d: %s", result.returncode, stderr)

            # The cached output may be stale (monitor moved to another port).
            if self._get_cached() and "unknown output" in stderr:
                self._set_cached(None)
                new_output = self.resolve()
                if new_output != output:
                    logger.info("Re-detected wlr-randr output: %s", new_output)
                    retry = subprocess.run(
                        [_WLR_BIN, "--output", new_output, "--on" if on else "--off"],
                        capture_output=True,
                        timeout=5,
                        env=_WLR_ENV,
                    )
                    return retry.returncode == 0
            return False
        except FileNotFoundError:
            logger.debug("wlr-randr not installed — cannot control display via Wayland")
            return False
        except Exception:
            logger.warning("wlr-randr failed", exc_info=True)
            return False

    def disable_empty_outputs(self) -> None:
        """Disable Wayland outputs that have no real monitor (no EDID).

        An unplugged port is still reported as ``enabled`` with a low-res
        fallback mode, which widens the logical screen and distorts the
        slideshow aspect ratio.  Skips entirely when ``METIXEL_WLR_OUTPUT``
        explicitly selects an output.
        """
        if self.override():
            return
        if not os.path.exists(_WLR_BIN):
            logger.debug("wlr-randr not installed — cannot clean up outputs")
            return
        try:
            result = subprocess.run(
                [_WLR_BIN, "--json"],
                capture_output=True,
                timeout=5,
                env=_WLR_ENV,
            )
            if result.returncode != 0:
                return
            outputs = json.loads(result.stdout.decode(errors="replace") or "[]")
        except Exception:
            logger.warning("Failed to enumerate outputs for cleanup", exc_info=True)
            return
        if not isinstance(outputs, list):
            return

        for out in outputs:
            if not out.get("enabled"):
                continue
            if out.get("make") or out.get("model"):
                continue  # real monitor — keep enabled
            name = out.get("name")
            if not isinstance(name, str):
                continue
            logger.info("Disabling phantom output (no monitor): %s", name)
            try:
                subprocess.run(
                    [_WLR_BIN, "--output", name, "--off"],
                    capture_output=True,
                    timeout=5,
                    env=_WLR_ENV,
                )
            except Exception:
                logger.warning("Failed to disable phantom output %s", name, exc_info=True)


class DisplayPower:
    """Three-tier display-power control (wlr-randr → DRM DPMS → vcgencmd)."""

    def __init__(self, wlr: WlrOutput) -> None:
        self._wlr = wlr

    def set(self, on: bool) -> None:
        """Set HDMI display power, trying each backend in order."""
        state = "on" if on else "off"
        on_off_flag = on

        # 1. wlr-randr (Wayland/wlroots — primary for cage on Trixie)
        if self._wlr.set_power(on_off_flag):
            logger.info("Display power (wlr-randr): %s", state.upper())
            return

        # 2. DRM DPMS sysfs (KMS fallback)
        if self._drm_dpms(state):
            logger.info("Display power (DRM DPMS): %s", state.upper())
            return

        # 3. vcgencmd (legacy Broadcom firmware / Bullseye)
        if not is_raspberry_pi():
            logger.warning("display_power: not on a Raspberry Pi — no-op")
            return

        cmd = ["vcgencmd", "display_power", "1" if on else "0"]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=5)
            logger.info("Display power (vcgencmd): %s", state.upper())
        except subprocess.CalledProcessError as e:
            logger.error("Failed to set display power: %s", e)
        except FileNotFoundError:
            logger.warning("vcgencmd not found — display power control unavailable")

    @staticmethod
    def _drm_dpms(state: str) -> bool:
        """Set display DPMS state via KMS sysfs. Returns True on success.

        Tries writing to ``/sys/class/drm/card*-*/dpms``.  On modern kernels
        these nodes may be read-only; falls back to ``sudo tee`` to
        ``.../status``.
        """
        # Try dpms node first (may be read-only on newer kernels)
        try:
            for card in glob.glob("/sys/class/drm/card*-*"):
                dpms_path = os.path.join(card, "dpms")
                if os.path.exists(dpms_path):
                    with open(dpms_path, "w") as f:
                        f.write(state)
                    return True
        except OSError:
            pass

        # Fallback: write to .../status via sudo tee
        try:
            for card in glob.glob("/sys/class/drm/card*-*"):
                status_path = os.path.join(card, "status")
                if os.path.exists(status_path):
                    on_off = "on" if state == "on" else "off"
                    result = subprocess.run(
                        ["sudo", "tee", status_path],
                        input=on_off,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if result.returncode == 0:
                        return True
        except Exception:
            pass

        return False


# Backwards-compatible module-level aliases used by callers/tests.
GpuInfoProvider = GpuInfo
WlrOutputManager = WlrOutput
DisplayPowerController = DisplayPower
