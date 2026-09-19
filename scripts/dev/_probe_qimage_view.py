#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Settle one question the software render path depends on, on real PySide6.

``MpvRenderWidget`` hands mpv the address of a ``bytearray`` and wraps the SAME
``bytearray`` in a ``QImage`` to upload it.  That only works if ``QImage`` takes a
*view* of the Python buffer.  If it copies, every frame would show the first one
and there would be no error to explain it — the picture would simply never move.

No display is needed (``QImage`` is not a widget), so this is safe to run over
ssh with no compositor.

Run:  python3 scripts/dev/_probe_qimage_view.py
"""

from __future__ import annotations

import ctypes
import sys


def main() -> int:
    try:
        from PySide6.QtGui import QImage
    except ImportError as exc:
        print(f"SKIP: PySide6 unavailable ({exc})")
        return 2

    width, height = 64, 48
    pixels = bytearray(width * height * 4)
    # The ctypes view is only needed to hand mpv a raw address; the QImage is
    # built from the bytearray itself.  Both must alias the same memory.
    backing = (ctypes.c_char * len(pixels)).from_buffer(pixels)
    address = ctypes.addressof(backing)

    image = QImage(pixels, width, height, width * 4, QImage.Format.Format_RGBX8888)
    print(f"pyside     : {__import__('PySide6').__version__}")
    print(f"image null : {image.isNull()}")
    print(f"image size : {image.width()}x{image.height()}  format={image.format()}")
    print(f"address    : {hex(address)}")

    before = image.pixelColor(0, 0).name()
    # Write pure red into the first pixel of the BYTEARRAY only.
    pixels[0:4] = bytes((255, 0, 0, 0))
    after = image.pixelColor(0, 0).name()

    print(f"pixel before write : {before}")
    print(f"pixel after  write : {after}")

    if after == "#ff0000":
        print("VERDICT: QImage VIEWS the bytearray (no copy) -- design is sound")
        return 0

    print("VERDICT: QImage did NOT see the write -- it copied, or the format is wrong")
    return 1


if __name__ == "__main__":
    sys.exit(main())
