# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Video / system metadata probes — ffprobe wrappers plus RAM and Pi model detection.

Extracted from ``VideoProcessor`` (Phase 2 decomposition) so the probe logic
is unit-testable in isolation.  Command lists live in ``ffmpeg_cmds``.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from metixel.backend.processing.ffmpeg_cmds import probe_cmd, validate_cmd
from metixel.backend.processing.utils import nice_cmd

logger = logging.getLogger(__name__)


def available_ram_bytes() -> int | None:
    """Read available system RAM from ``/proc/meminfo``.

    Returns ``MemAvailable`` in bytes, or ``None`` if the file cannot
    be read (e.g. on a non-Linux dev machine).
    """
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024  # kB → bytes
    except (OSError, ValueError):
        pass
    return None


def detect_pi_model() -> str | None:
    """Detect the Raspberry Pi model from ``/proc/device-tree/model``.

    Returns the profile key (``pi2``, ``pi3``, ``pi4``, ``pi5``) or
    ``None`` if the model can't be determined.
    """
    try:
        with open("/proc/device-tree/model") as f:
            model = f.read().strip("\x00").strip()
    except (OSError, FileNotFoundError):
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
        return "pi3"  # Zero 2 W has similar VideoCore IV to Pi 3
    return None


def probe_video(path: Path, timeout: int) -> dict:
    """Probe a video file for metadata using ffprobe.

    Returns a dict with keys: width, height, duration, codec_name,
    fps, bitrate, color_depth, h264_profile, h264_level,
    color_primaries, color_trc, colorspace, pix_fmt.
    """
    cmd = nice_cmd(probe_cmd(path))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    data = json.loads(result.stdout)
    info: dict = {
        "width": 0,
        "height": 0,
        "duration": 0.0,
        "codec_name": "",
        "fps": 0.0,
        "bitrate": 0,
        "color_depth": 8,
        "h264_profile": "",
        "h264_level": "",
        "color_primaries": "",
        "color_trc": "",
        "colorspace": "",
        "pix_fmt": "",
    }
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            info["width"] = stream.get("width", 0)
            info["height"] = stream.get("height", 0)
            info["codec_name"] = stream.get("codec_name", "")
            info["pix_fmt"] = stream.get("pix_fmt", "")
            info["h264_profile"] = stream.get("profile", "")
            info["h264_level"] = stream.get("level", "")
            # Normalise ffprobe level: integer 40 → float 4.0
            if isinstance(info["h264_level"], int) and info["h264_level"] > 9:
                info["h264_level"] = float(info["h264_level"]) / 10.0
            elif info["h264_level"]:
                try:
                    info["h264_level"] = float(info["h264_level"])
                except (ValueError, TypeError):
                    info["h264_level"] = ""
            info["color_primaries"] = stream.get("color_primaries", "")
            info["color_trc"] = stream.get("color_transfer", "")
            info["colorspace"] = stream.get("color_space", "")

            # Framerate
            fps_str = stream.get("r_frame_rate", "0/1")
            try:
                num, den = fps_str.split("/")
                info["fps"] = round(float(num) / float(den), 2)
            except (ValueError, ZeroDivisionError):
                info["fps"] = 0.0

            # Bitrate (prefer stream-level, fall back to format-level)
            if stream.get("bit_rate"):
                info["bitrate"] = int(stream["bit_rate"]) // 1_000_000
            break

    # Format-level bitrate fallback
    fmt = data.get("format", {})
    if info["bitrate"] == 0 and fmt.get("bit_rate"):
        info["bitrate"] = int(fmt["bit_rate"]) // 1_000_000
    info["duration"] = float(fmt.get("duration", 0))

    # Detect color depth from pixel format
    pf = info["pix_fmt"]
    if pf and "10" in pf:
        info["color_depth"] = 10
    elif pf and "12" in pf:
        info["color_depth"] = 12

    return info


def validate_cached_video(path: Path, timeout: int) -> bool:
    """Check that a cached video file is valid (not corrupt/partial).

    Runs a quick ``ffprobe`` to verify the file has a readable video
    stream.  Returns ``True`` if the file is valid.

    The timeout is generous (60s) because on CPU-starved Pi 2/3
    hardware, ffprobe can take 20–30s just to open a file when another
    transcode is saturating the I/O and CPU.
    """
    try:
        result = subprocess.run(
            nice_cmd(validate_cmd(path)),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0 and "video" in result.stdout.lower()
    except subprocess.TimeoutExpired:
        logger.warning(
            "ffprobe timed out validating cached video — system may be overloaded: %s",
            path.name,
        )
        return False
    except OSError:
        return False
