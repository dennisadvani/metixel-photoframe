# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Raspberry Pi platform detection and ``vcgencmd`` helpers.

Consolidates the duplicated ``/proc/device-tree/model`` reads (probe helpers,
display backend auto-detection, system info) and the repeated
``vcgencmd get_mem`` invocations.  All functions return safe defaults on
non-Linux / non-Pi machines.
"""

from __future__ import annotations

import os
import socket
import subprocess


def read_device_tree_model() -> str | None:
    """Read the model string from ``/proc/device-tree/model``.

    Returns ``None`` if the file is unavailable (non-Pi or non-Linux).
    """
    try:
        with open("/proc/device-tree/model") as f:
            return f.read().strip("\x00\n\t ")
    except (OSError, FileNotFoundError):
        return None


def is_raspberry_pi(legacy_fallback: bool = True) -> bool:
    """Return ``True`` if the device is a Raspberry Pi.

    Reads ``/proc/device-tree/model``; when that's unavailable (legacy
    Bullseye images), falls back to the presence of ``/opt/vc/lib``.
    """
    model = read_device_tree_model()
    if model is not None:
        return "Raspberry Pi" in model
    if legacy_fallback:
        return os.path.exists("/opt/vc/lib/libEGL.so")
    return False


def detect_pi_model() -> str | None:
    """Detect the Raspberry Pi model as a transcode profile key.

    Returns ``pi2``, ``pi3``, ``pi4``, or ``pi5`` — or ``None`` if the
    model can't be determined.  A Pi Zero 2 W maps to ``pi3`` (similar
    VideoCore IV to the Pi 3).
    """
    model = read_device_tree_model()
    if model is None:
        return None
    model_lower = model.lower()
    if "raspberry pi 5" in model_lower:
        return "pi5"
    if "raspberry pi 4" in model_lower or "raspberry pi 400" in model_lower:
        return "pi4"
    if "raspberry pi 3" in model_lower:
        return "pi3"
    if "raspberry pi 2" in model_lower:
        return "pi2"
    if "raspberry pi zero 2" in model_lower:
        return "pi3"
    return None


#: mpv ``--hwdec`` value per Pi model, measured on hardware.
#:
#: **These must be set EXPLICITLY; ``auto`` is wrong on both platforms.**  ``auto``
#: walks a fixed probe order (CUDA → Vulkan → VAAPI → drm → …) and on a Pi 3 it
#: exhausts every candidate without ever reaching ``v4l2m2m``, landing on software
#: at 170% CPU — *worse* than requesting no hardware decoding at all (138%).
#:
#: Measured (1080p HEVC on a Pi 5, 720p H.264 on a Pi 3, services stopped):
#:
#: ======= ======== ============ =====================
#: Board   Codec    Working      Measured CPU
#: ======= ======== ============ =====================
#: Pi 5    HEVC     ``drm-copy`` 49% software → 12%
#: Pi 5    H.264    *(none)*     always software
#: Pi 3    H.264    ``v4l2m2m``  138% software → 55%
#: Pi 3    HEVC     *(none)*     always software
#: ======= ======== ============ =====================
#:
#: The asymmetry is real and not a misconfiguration: a Pi 5's V3D exposes V4L2
#: HEVC stateless via ``rpi-hevc-dec`` (``/dev/video19``) with DMABuf interop,
#: while the Pi 3's VC4 offers only ``bcm2835-codec-decode`` (``/dev/video10``,
#: H.264) and lacks the dmabuf interop ``drm-copy`` requires.
#:
#: This mapping deliberately mirrors :data:`~metixel.backend.processing.video.
#: VideoProcessor.PROFILES` — the board whose profile transcodes to H.265 is the
#: board that can hardware-decode H.265.  Deriving both from the same model keeps
#: the codec and the decoder from ever disagreeing, which is the failure that
#: would otherwise ship silently: a board transcoding to a codec it cannot
#: hardware-decode looks fine until the CPU saturates.
HWDecByModel: dict[str, str] = {
    # `drm-copy` names the interop layer, not a software path — underneath it
    # libavcodec drives the V4L2 HEVC stateless device with DMABuf both ways.
    # Plain `drm` (zero-copy) SILENTLY falls back to software for HEVC.
    "pi5": "drm-copy",
    "pi4": "drm-copy",
    # VC4 decodes H.264 through the mem2mem device. `drm`/`drm-copy` do not work
    # here, and `auto` never reaches this path at all.
    "pi3": "v4l2m2m",
    "pi2": "v4l2m2m",
}


def hwdec_for_model(model: str | None) -> str:
    """Return the mpv ``--hwdec`` value for *model*, or ``"no"`` if unknown.

    ``"no"`` is the honest answer when the board cannot be identified: it asks mpv
    for software decoding explicitly, rather than leaving it to ``auto``, which on
    a Pi 3 measurably costs *more* CPU than plain software.  A wrong guess here is
    not a crash — it is a frame that runs hot and stutters, which is far harder to
    diagnose than a failed start.
    """
    if model is None:
        return "no"
    return HWDecByModel.get(model, "no")


def resolve_unique_id() -> str:
    """Return a stable, hardware-unique identifier for this device.

    Resolution order (first hit wins):

    1. **Raspberry Pi serial number** (``/proc/device-tree/serial-number``)
       — factory-burned per physical board, so it survives SD-card cloning
       and is unique across every Pi ever produced.
    2. **First non-loopback MAC address** (``/sys/class/net/*/address``) —
       used on non-Pi SBCs (Phase 2) and where the serial isn't exposed.
    3. **systemd machine-id** (``/etc/machine-id``) — a stable per-OS-install
       id on systems without a readable hardware serial/MAC.
    4. **Hostname** — last resort (not guaranteed unique, but better than
       nothing).

    Used to scope the MQTT topics and Home Assistant device identity so two
    frames on one broker never collide, even when both run the default config
    and leave ``mqtt.device_id`` empty.
    """
    # 1. Raspberry Pi serial number — the canonical per-board id.
    for path in (
        "/proc/device-tree/serial-number",
        "/sys/firmware/devicetree/base/serial-number",
    ):
        try:
            with open(path) as f:
                serial = f.read().strip("\x00\n\t ")
            if serial:
                return serial
        except (OSError, FileNotFoundError):
            continue

    # 2. First non-loopback MAC address.
    try:
        import glob

        for path in sorted(glob.glob("/sys/class/net/*/address")):
            try:
                with open(path) as f:
                    mac = f.read().strip()
            except OSError:
                continue
            if mac and mac != "00:00:00:00:00:00":
                return mac.replace(":", "").lower()
    except OSError:
        pass

    # 3. systemd machine-id.
    try:
        with open("/etc/machine-id") as f:
            machine_id = f.read().strip()
        if machine_id:
            return machine_id
    except (OSError, FileNotFoundError):
        pass

    # 4. Hostname.
    return socket.gethostname() or "metixel"


def boot_identity() -> str:
    """Return an identifier for the current boot of this machine.

    Unlike :func:`resolve_unique_id` (which identifies the *device*), this
    identifies the *boot*: it changes on every reboot and is stable in
    between.  Combined with a process id it is the basis for the frontend
    liveness check — a value that does NOT embed a timestamp, because such a
    value would be ambiguous once the file it lives in is cleared by a reboot
    (straight-after-reboot would be indistinguishable from stale).

    Resolution order:

    1. **``/proc/sys/kernel/random/boot_id``** — a fresh UUID per boot on
       Linux, which is exactly the contract wanted here.
    2. **Kernel boot time** (``btime`` in ``/proc/stat``) — also changes per
       boot and needs no flag day, so an older kernel still works.
    3. **``"unknown"``** — on systems exposing neither (desktop dev on
       Windows/macOS).  Callers that need a stable identity outside Linux
       fall back to the process id; see :class:`metixel.backend.frontend_liveness`.

    Deliberately NOT read from ``/etc/machine-id``: that is per OS install,
    not per boot, so every reboot would look like the same process.
    """
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            boot_id = f.read().strip()
        if boot_id:
            return boot_id
    except (OSError, FileNotFoundError):
        pass

    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    return line.split()[1]
    except (OSError, FileNotFoundError):
        pass

    return "unknown"


def read_vcgencmd_mem(unit: str) -> int | None:
    """Run ``vcgencmd get_mem <unit>`` and return the value in MB.

    Returns ``None`` if vcgencmd is unavailable or the output can't be
    parsed (e.g. ``"gpu=512M"`` → ``512``).
    """
    try:
        result = subprocess.run(
            ["vcgencmd", "get_mem", unit],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and "=" in result.stdout:
            val = result.stdout.strip().split("=")[-1].rstrip("M")
            return int(val)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return None


def read_vcgencmd_mem_str(unit: str, fallback: str = "unknown") -> str:
    """Run ``vcgencmd get_mem <unit>`` and return the raw output line.

    e.g. ``"gpu=512M"``.  Returns ``fallback`` if vcgencmd is unavailable.
    """
    try:
        result = subprocess.run(
            ["vcgencmd", "get_mem", unit],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return fallback
