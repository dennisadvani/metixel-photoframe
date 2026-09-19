#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Localise the "black video rectangle" failure to mpv's side or Qt's side.

The widget on the Pi reveals the artwork hole correctly but paints black inside
it.  Two very different causes look identical on screen:

  * mpv is not writing pixels into our buffer (a params/pointer/stride bug in the
    production marshalling), or
  * mpv writes them fine and Qt does not get them onto the screen (a paint or
    z-order problem).

This drives the PRODUCTION code path (``metixel.display.sw_render``) with no Qt
widget at all, so the buffer contents can be inspected directly.  If the buffer
comes back lit, the fault is on the Qt side; if it comes back black, it is in the
marshalling.

Run on the Pi:  PYTHONPATH=/opt/metixel/live/src python3 _probe_sw_production_path.py
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import sys
import time

from metixel.display.sw_render import (
    DEFAULT_SW_FORMAT,
    build_sw_params,
    bytes_per_pixel,
    install_sw_render_params,
    sw_target_size,
)

VIDEO = sys.argv[1] if len(sys.argv) > 1 else "/opt/metixel/data/cache/videos/2cd3ac6ece47670f.mp4"
WIDGET_W, WIDGET_H = 1905, 1185
MAX_PIXELS = 1_000_000
FRAMES = 5


def main() -> int:
    try:
        import mpv as mpvlib
    except ImportError as exc:
        print(f"SKIP: python-mpv unavailable ({exc})")
        return 2

    install_sw_render_params(mpvlib)
    print(f"registered sw params: {sorted(mpvlib.MpvRenderParam.TYPES)[-4:]}")

    player = mpvlib.MPV(
        vo="libmpv",
        hwdec="drm-copy",
        mute=True,
        loop=False,
        keep_open="no",
        osc=False,
        osd_level=0,
        input_default_bindings=False,
        input_vo_keyboard=False,
        loglevel="warn",
    )
    ctx = mpvlib.MpvRenderContext(player, "sw")

    # Exactly what MpvRenderWidget does.
    buf_w, buf_h = sw_target_size(WIDGET_W, WIDGET_H, MAX_PIXELS)
    bpp = bytes_per_pixel(DEFAULT_SW_FORMAT)
    pixels = bytearray(buf_w * buf_h * bpp)
    backing = (ctypes.c_char * len(pixels)).from_buffer(pixels)
    address = ctypes.addressof(backing)
    params = build_sw_params(buf_w, buf_h, address, DEFAULT_SW_FORMAT)

    print(f"widget {WIDGET_W}x{WIDGET_H} -> buffer {buf_w}x{buf_h} ({buf_w * buf_h} px)")
    print(f"format={DEFAULT_SW_FORMAT} bpp={bpp} bytes={len(pixels)} address={hex(address)}")
    print(f"params: size={params['sw_size']} stride={params['sw_stride']}")

    player.play(VIDEO)
    time.sleep(3.0)

    for index in range(FRAMES):
        try:
            ctx.render(**params)
        except Exception as exc:  # noqa: BLE001 - the failure is the result
            print(f"RENDER FAILED: {type(exc).__name__}: {exc}")
            return 2
        raw = bytes(pixels)
        lit = len(raw) - raw.count(0)
        print(
            f"frame {index}: md5={hashlib.md5(raw).hexdigest()[:12]} lit_bytes={lit} / {len(raw)}"
        )
        time.sleep(0.2)

    raw = bytes(pixels)
    lit = len(raw) - raw.count(0)

    # Does a QImage built from the SAME bytearray see the pixels?
    try:
        from PySide6.QtGui import QImage

        image = QImage(pixels, buf_w, buf_h, buf_w * bpp, QImage.Format.Format_RGBX8888)
        centre = image.pixelColor(buf_w // 2, buf_h // 2)
        corner = image.pixelColor(2, 2)
        image_lit = 0
        for y in range(0, buf_h, max(1, buf_h // 20)):
            for x in range(0, buf_w, max(1, buf_w // 20)):
                if image.pixelColor(x, y).lightness() > 4:
                    image_lit += 1
        print(f"QImage centre={centre.name()} corner={corner.name()} sampled_lit={image_lit}")
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        print(f"QImage check failed: {type(exc).__name__}: {exc}")

    with contextlib.suppress(Exception):
        ctx.free()

    print()
    if lit > len(raw) // 100:
        print(f"VERDICT: mpv DID fill our buffer ({lit} lit bytes) — fault is on the Qt side")
    else:
        print(f"VERDICT: buffer is BLACK ({lit} lit bytes) — fault is in the marshalling")
    return 0


if __name__ == "__main__":
    sys.exit(main())
