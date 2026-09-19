# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Software-render plumbing for the libmpv render API.

Why this exists
---------------
metixel draws video through libmpv's render API into a Qt widget.  With the
default ``opengl`` API type that path leaks one ``anon_inode:sync_file``
descriptor per :meth:`MpvRenderContext.render` call on the Pi's stack — measured
at ~28-30/s, i.e. ``fd 1020 / limit 1024`` after ~27 s of *cumulative* playback,
after which the frontend dies of ``OSError: [Errno 24] Too many open files``.

The leak is **per draw call**, not per frame, per second or per decoder:

* it survives ``hwdec=no`` (~24/s), ``vd-lavc-dr=no`` and ``--gpu-context=drm``;
* it scales with the draw rate (``speed=0.1`` cut it 10x);
* a still image does not leak at all, and decoding alone does not leak;
* Qt's own presentation path is clean across five widget shapes (510-541 frames,
  ``+0.0/s``);
* ``--vo=gpu`` is clean while ``--vo=gpu-next`` and the render API both leak.

A backport of the upstream libplacebo sync-object fix (``c93aa134``) was built,
verified as genuinely recompiled (disassembly differs, soname unchanged) and
measured to leak *identically* to Debian's — so that commit is not the cause.

``MPV_RENDER_API_TYPE_SW`` bypasses the GL fence path entirely: mpv converts the
frame into a host-provided CPU buffer.  Measured clean (``+0.0/s``) at four
buffer formats and two resolutions, with hardware decoding still active
(``hwdec=drm-copy``), and with every rendered frame verified three ways (distinct
md5 per sample, non-zero byte count, and a dumped PNG showing a correctly
letterboxed picture).

This module holds the parts that need **neither Qt nor mpv**, so they can be
unit-tested on a machine that has neither — which is the build machine:

* the four ``sw_*`` render parameters python-mpv does not know about;
* the buffer format table;
* the buffer-size cap, which is the only cost lever that measurably matters.
"""

from __future__ import annotations

import ctypes
import math
from ctypes import c_int, c_size_t, c_void_p
from typing import Any

#: ``mpv_render_param_type`` ids for the software-render parameters, taken from
#: libmpv's ``render.h``.  python-mpv's own ``TYPES`` mapping stops at
#: ``drm_display_v2`` (16), so 17-20 are the ones it cannot build.
SW_PARAM_IDS: dict[str, int] = {
    "sw_size": 17,
    "sw_format": 18,
    "sw_stride": 19,
    "sw_pointer": 20,
}

#: Requestable buffer formats and their bytes per pixel.
#:
#: The format is *not* a CPU cost lever — measured on a Pi 5 at 1904x1184,
#: halving the bytes written (``rgb565``) moved CPU from 110% to 111% of one
#: core, and ``rgb24`` was *slower* at 118%.  The time goes on mpv's scale and
#: colour conversion, not on the write.  It remains useful as a memory and
#: upload-bandwidth lever on constrained boards.
#:
#: ``rgb0`` is the default: packed R,G,B with an unused fourth byte, which maps
#: 1:1 onto ``QImage.Format_RGBX8888`` for a byte-order-agnostic upload.
SW_FORMATS: dict[str, int] = {
    "rgb0": 4,
    "bgr0": 4,
    "rgba": 4,
    "bgra": 4,
    "rgb24": 3,
    "bgr24": 3,
    "rgb565": 2,
    "rgb555": 2,
}

DEFAULT_SW_FORMAT = "rgb0"

#: Smallest sensible buffer edge.  A zero or one pixel buffer is not a video
#: frame, and a zero would be a divide-by-zero in the aspect maths below.
MIN_SW_DIMENSION = 2


class _SwSize(ctypes.Structure):
    """``int sw_size[2]`` — the geometry of the buffer the host provides."""

    _fields_ = [("width", c_int), ("height", c_int)]


class _SwStride(ctypes.Structure):
    """``size_t sw_stride`` — bytes per row of that buffer."""

    _fields_ = [("value", c_size_t)]


def install_sw_render_params(mpvlib: Any) -> None:  # noqa: ANN401 - a module
    """Teach a python-mpv module about the four software-render parameters.

    python-mpv builds each render parameter through ``MpvRenderParam.TYPES``, so
    a name that is missing raises ``ValueError: unknown render param type``
    *before* libmpv is reached.  The Pi ships python3-mpv 1.0.7, whose ``TYPES``
    ends at ``drm_display_v2`` (id 16) — ``grep -c sw_size`` on the installed
    module returns 0 — so without this call the software path creates a render
    context it can never draw with.

    The parameter *types* matter as much as the ids, because python-mpv coerces
    the caller's value through the registered type: ``sw_format`` is a ``char*``
    and reuses python-mpv's own ``str`` handler, ``sw_pointer`` is a ``void*``
    and reuses ``c_void_p``, and the two scalars need small ``Structure``
    wrappers since python-mpv only knows how to build a ``Structure`` from a
    mapping.

    The module is passed in rather than imported so this is testable against a
    stub, and so importing :mod:`metixel.display.sw_render` never pulls in mpv.

    Idempotent: re-registering the same names is harmless, which matters because
    the widget may recreate its render context after a stop.
    """
    mpvlib.MpvRenderParam.TYPES["sw_size"] = (SW_PARAM_IDS["sw_size"], _SwSize)
    mpvlib.MpvRenderParam.TYPES["sw_format"] = (SW_PARAM_IDS["sw_format"], str)
    mpvlib.MpvRenderParam.TYPES["sw_stride"] = (SW_PARAM_IDS["sw_stride"], _SwStride)
    mpvlib.MpvRenderParam.TYPES["sw_pointer"] = (SW_PARAM_IDS["sw_pointer"], c_void_p)


def bytes_per_pixel(fmt: str = DEFAULT_SW_FORMAT) -> int:
    """Return the bytes per pixel for *fmt*.

    Raises ``KeyError`` for an unknown format rather than guessing: a wrong
    stride produces a sheared picture, not an error, and that is much harder to
    diagnose than a failed start.
    """
    return SW_FORMATS[fmt]


def sw_target_size(width: int, height: int, max_pixels: int) -> tuple[int, int]:
    """Return the buffer size to ask mpv for, given the widget *width* x *height*.

    The software renderer costs CPU in mpv's own scale-and-convert step, and that
    cost is what limits the frame rate.  Measured on a Pi 5 at 30 fps, services
    stopped:

    ============ ======== ========= ==========
    buffer       megapixel  of a core ms/frame
    ============ ======== ========= ==========
    476x296      0.14       62%       20.7
    952x592      0.56       80%       26.7
    1280x800     1.02       82%       27.4
    1904x1184    2.25      110%       37.0
    ============ ======== ========= ==========

    Splitting the cost by draw rate (30 fps vs 15 fps, same buffer: 13.15 s vs
    9.05 s of CPU) separates a **30%-of-a-core continuous floor** — decode,
    demux and audio at realtime, which no render mode can remove — from a
    ~10 ms fixed plus ~7.4 ms/Mpx cost per ``render()`` call.  Two consequences
    drive this function:

    1. rendering at the full artwork rectangle cannot sustain 30 fps (110% of a
       core, 27 fps ceiling), so the buffer must be capped and the GPU left to do
       the final scale;
    2. the fixed per-call term means shrinking below ~0.5 Mpx buys almost
       nothing, so there is no point capping hard.

    The aspect ratio is preserved exactly, so mpv's own ``panscan`` / letterbox
    decision lands on the same pixels it would at full size — only the sampling
    resolution changes.  A widget already within budget is returned unchanged;
    this never upscales, and never returns a dimension below
    :data:`MIN_SW_DIMENSION`.

    ``max_pixels <= 0`` means "uncapped", for tests and for the desktop backend.
    """
    width = max(MIN_SW_DIMENSION, int(width))
    height = max(MIN_SW_DIMENSION, int(height))
    if max_pixels <= 0 or width * height <= max_pixels:
        return width, height

    scale = math.sqrt(max_pixels / (width * height))
    target_w = max(MIN_SW_DIMENSION, int(width * scale))
    target_h = max(MIN_SW_DIMENSION, int(height * scale))
    return target_w, target_h


def build_sw_params(
    width: int,
    height: int,
    pointer: int,
    fmt: str = DEFAULT_SW_FORMAT,
) -> dict[str, Any]:
    """Build the keyword arguments for one software ``render()`` call.

    Returned as a mapping because python-mpv's ``render(**kwargs)`` takes one and
    each entry carries its own type id, so the order is irrelevant.

    ``pointer`` is the address of the host buffer.  The stride is that buffer's
    row pitch, so it must agree with the format named here — a mismatch shows up
    as a sheared picture, not as an error.
    """
    return {
        "sw_size": {"width": int(width), "height": int(height)},
        "sw_format": fmt,
        "sw_stride": {"value": int(width) * bytes_per_pixel(fmt)},
        "sw_pointer": c_void_p(int(pointer)),
    }
