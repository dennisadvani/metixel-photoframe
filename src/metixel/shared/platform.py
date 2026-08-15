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
