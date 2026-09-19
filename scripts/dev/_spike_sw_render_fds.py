#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""SPIKE — does libmpv's SOFTWARE render API leak ``sync_file`` descriptors?

The frontend's video path leaks one DMA-BUF ``sync_file`` descriptor per
``MpvRenderContext.render()`` call (``sync_file == paints + 1`` in every leaking
run) and dies of ``Too many open files`` after ~27s of cumulative playback.  The
leak has been narrowed by elimination to libplacebo's GL draw path:

* not metixel's code — a per-tick ``setGeometry`` guard changed nothing;
* not hardware decoding — ``hwdec=no`` leaks too, ``decode errors: 0``;
* not Qt — five bare widget shapes at 30fps, no mpv in the process, are clean;
* not the Wayland WSI — ``--vo=gpu-next --gpu-context=drm`` leaks identically
  with no compositor running at all;
* not a libplacebo GLsync bug — backporting ``c93aa13`` (verified compiled into
  the loaded ``.so``) left the rate unchanged.

What IS established: ``--vo=gpu`` (mpv's own shader renderer) is clean while
``--vo=gpu-next`` (libplacebo) leaks, and the libmpv render API inherits the leak
because ``vo=libmpv`` is libplacebo-backed in mpv 0.40.  So the question this
spike answers is narrow and decisive:

    **If we stop going through a GL draw call at all, does the leak stop?**

libmpv has a second render API for exactly that — ``MPV_RENDER_API_TYPE_SW``,
where mpv hands back finished frames in system memory and the host uploads them.
It never touches EGL, never creates a GL swapchain, and therefore should never
create the fences we believe are leaking.  That is a *hypothesis*; this spike
measures it.

Why a shim is needed (and why this is not a one-liner)
----------------------------------------------------
python-mpv passes ``api_type`` through as a free string, so
``MpvRenderContext(player, "sw")`` succeeds in creating the context.  But
``render(**kwargs)`` builds each parameter through ``MpvRenderParam(name, value)``,
which raises ``ValueError("unknown render param type ...")`` for any name missing
from ``MpvRenderParam.TYPES``.  That dict stops at ``drm_display_v2`` (id 16); the
software params are ids 17-20 and are simply absent, so the context can be created
but never drawn into.  This spike adds the four entries for the lifetime of the
process.  Confirmed against the shipped module on the Pi:

    $ python3 -c 'import mpv; print(sorted(mpv.MpvRenderParam.TYPES))'
    [... 'advanced_control', 'ambient_light', 'api_type', 'block_for_target_time',
     'depth', 'drm_display', 'drm_display_v2', 'drm_draw_surface_size', 'flip_y',
     'icc_profile', 'invalid', 'next_frame_info', 'opengl_fbo',
     'opengl_init_params', 'skip_rendering', 'wl_display', 'x11_display']

No Qt is imported anywhere in this file.  That is deliberate: the point is to
vary ONE thing (GL draw call vs software conversion) against the standing result
that Qt's own presentation path is clean, so Qt must not be in the picture.

What a result means
-------------------
* ``sync_file`` flat AND the buffer is non-blank and changing -> the leak belongs
  to the GL draw call, and the software render API is a viable escape (at a CPU
  cost to be measured next).
* ``sync_file`` still climbing -> the descriptor is not created by the GL path,
  and the remaining routes are GStreamer or waiting for upstream.
* ``RENDER FAILED`` -> the software API is unusable here and the question is moot.

The "AND the buffer is non-blank and changing" clause is not decoration.  A flat
``sync_file`` count on its own only proves no fences were created; a render call
that quietly converts nothing produces exactly the same flat count.  So every
sample also fingerprints the destination buffer, and a run cannot be reported as
clean unless real pixels are arriving.  (This project has already been misled once
by byte-identical captures of a video that was not actually rendering.)

One process per measurement: descriptors are never returned, so a leaking run
poisons the baseline for the next one.

Usage (on the Pi; ``cage`` is not required but keeps the environment the same as
production, and the supplementary groups matter for any hardware-decode path):

    python3 _spike_sw_render_fds.py \
        --video /opt/metixel/data/cache/videos/2cd3ac6ece47670f.mp4 \
        --hwdec drm-copy --size 1920x1200
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import sys
import time

#: Parameter ids from libmpv's ``render.h``, in the same numbering scheme
#: python-mpv already uses for ids 0-16.  Kept as one mapping so a mismatch
#: between an id and its type is visible in one place.
SW_PARAM_IDS: dict[str, int] = {
    "sw_size": 17,
    "sw_format": 18,
    "sw_stride": 19,
    "sw_pointer": 20,
}

#: Formats the software renderer can be asked for, with their bytes per pixel.
#:
#: ``rgb0`` is packed R,G,B with an unused fourth byte — the cheapest thing mpv
#: will hand back that Qt can wrap without a further conversion.  At 1920x1200
#: that is 9.2 MB per frame, ~276 MB/s at 30fps, so the write cost is real.  The
#: narrower formats exist to test whether that bandwidth is where the CPU time
#: goes: ``rgb565`` halves it and matches the ``GL_RGB565`` texture the
#: production backend is meant to use (2 bytes/pixel instead of 4).
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

#: Default format, and what every earlier measurement with this probe used.
SW_FORMAT = "rgb0"

#: Sampling cadence and warm-up.  Warm-up matters because the render context and
#: the decoder allocate a burst of descriptors on the first few frames; counting
#: those as "the leak" would manufacture a result.
WARMUP_SECONDS = 2.5
SAMPLE_EVERY_SECONDS = 2.0

#: Growth (in descriptors) above which a run is called leaking.  The GL path
#: grows ~28-30/s, so even one second's worth is far outside this threshold,
#: while startup noise is not.
LEAK_THRESHOLD = 10


class _SwSize(ctypes.Structure):
    """``int sw_size[2]`` — the geometry of the buffer the host provides."""

    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int)]


class _SwStride(ctypes.Structure):
    """``size_t sw_stride`` — bytes per row of that buffer."""

    _fields_ = [("value", ctypes.c_size_t)]


def _install_sw_render_params() -> None:
    """Teach python-mpv the four software-render params libmpv already supports.

    The parameter *types* are what matter, not just the ids: python-mpv's
    ``MpvRenderParam.__init__`` looks up an entry and then coerces the caller's
    value through it.  ``sw_format`` is a ``char*`` and can reuse python-mpv's
    existing ``str`` handler; ``sw_pointer`` is a ``void*`` and can reuse the
    ``c_void_p`` handler; the two remaining params point at scalars, so they need
    small wrapper structs because python-mpv only knows how to build a
    ``Structure`` from a mapping.
    """
    import mpv as mpvlib

    mpvlib.MpvRenderParam.TYPES["sw_size"] = (SW_PARAM_IDS["sw_size"], _SwSize)
    mpvlib.MpvRenderParam.TYPES["sw_format"] = (SW_PARAM_IDS["sw_format"], str)
    mpvlib.MpvRenderParam.TYPES["sw_stride"] = (SW_PARAM_IDS["sw_stride"], _SwStride)
    mpvlib.MpvRenderParam.TYPES["sw_pointer"] = (
        SW_PARAM_IDS["sw_pointer"],
        ctypes.c_void_p,
    )


def _fd_counts() -> tuple[int, int]:
    """Return ``(total_fds, sync_file_fds)`` for THIS process.

    Counted from ``/proc/self/fd`` rather than by summing leak suspects, so the
    total cannot be wrong by omission.  A descriptor closed under us is normal
    and is skipped rather than raising.
    """
    total = 0
    sync = 0
    for name in os.listdir("/proc/self/fd"):
        total += 1
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue
        if "sync_file" in target:
            sync += 1
    return total, sync


def _fingerprint(raw: bytes) -> tuple[str, int]:
    """Return ``(md5, non_zero_bytes)`` for one rendered frame buffer.

    A flat ``sync_file`` count only proves no fences were created — a render call
    that converts nothing looks identical.  Hashing the destination and counting
    its non-zero bytes is what separates "the software path draws real frames
    without leaking" from "the software path draws nothing at all".

    ``bytes.count`` runs at C speed; a Python-level scan of a multi-megabyte
    buffer on every sample would itself distort the measurement.
    """
    return hashlib.md5(raw).hexdigest()[:12], len(raw) - raw.count(0)


def _render_params(width: int, height: int, pointer: ctypes.c_void_p, fmt: str = SW_FORMAT) -> dict:
    """Build the keyword arguments the software renderer needs for one frame.

    Returned as a mapping because python-mpv's ``render(**kwargs)`` takes one, and
    each entry carries its own type id so the order is irrelevant.  The stride is
    the row pitch of OUR buffer, so it must agree with the format named here — a
    mismatch shows up as a sheared picture, not as an error.
    """
    return {
        "sw_size": {"width": width, "height": height},
        "sw_format": fmt,
        "sw_stride": {"value": width * SW_FORMATS[fmt]},
        "sw_pointer": pointer,
    }


def _report(
    player: object,
    samples: list[tuple[float, int, int, int, str, int]],
    fmt: str = SW_FORMAT,
) -> None:
    """Print the trajectory, the mpv-decided properties, and the verdict."""
    if len(samples) < 2:
        print("VERDICT: inconclusive — fewer than two samples were taken", flush=True)
        return

    first, last = samples[0], samples[-1]
    span = last[0] - first[0]
    grew = last[2] - first[2]
    rate = grew / span if span > 0 else 0.0

    # "Are real frames arriving?" is answered by the buffer, not by the frame
    # counter — the counter only says how often render() was called.
    digests = {sample[4] for sample in samples}
    lit = [sample[5] for sample in samples]
    frames_are_real = len(digests) > 1 and max(lit) > 0

    print()
    print(f"frames drawn     : {last[1]}")
    print(f"sample window    : {span:.1f}s")
    print(f"sync_file growth : {grew} ({rate:+.1f}/s)")
    print(f"distinct frames  : {len(digests)} of {len(samples)} samples")
    print(f"lit bytes        : min={min(lit)} max={max(lit)}")
    print(f"frames verified  : {frames_are_real}")

    # Which path mpv actually chose.  Without this, a clean result could be
    # misread as "software rendering avoids the leak" when in fact hardware
    # decode had silently fallen back and the comparison was not the one asked
    # for.  Attribute access is required, NOT player[name]: __getitem__ looks
    # under options/ and reports every one of these as "does not exist".
    print("mpv decided:")
    for attr in ("hwdec_current", "width", "height", "core_idle"):
        try:
            print(f"  {attr:28s} = {getattr(player, attr)}")
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            print(f"  {attr:28s} = ? ({exc})")
    try:
        print(f"  {'video_params':28s} = {player.video_params}")  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        print(f"  {'video_params':28s} = ? ({exc})")

    # Order matters: a blank render must never be reported as clean, so the
    # "nothing was drawn" case is checked FIRST and short-circuits the verdict.
    if not frames_are_real:
        verdict = "INCONCLUSIVE — nothing was rendered"
    elif grew > LEAK_THRESHOLD:
        verdict = "LEAKS"
    else:
        verdict = "clean"
    print()
    print(f"VERDICT [{fmt}]: {verdict}")


#: How Pillow should read each format's bytes back out of the raw buffer.
#: The ``0`` variants are packed RGB with a trailing byte Pillow drops via the raw
#: mode string.  16-bit packed formats have no Pillow raw mode, so they cannot be
#: dumped — stated explicitly rather than silently producing a wrong image.
_PILLOW_RAW_MODES = {"rgb0": "RGBX", "bgr0": "BGRX", "rgb24": "RGB", "bgr24": "BGR"}


def _dump_frame(raw: bytes, width: int, height: int, path: str, fmt: str = SW_FORMAT) -> None:
    """Write the destination buffer as a PNG so the frame can be LOOKED at.

    A fingerprint proves the bytes change; only an image proves they are a
    picture.  ``rgb0``/``bgr0`` are packed R,G,B with an unused fourth byte, so
    Pillow is told to drop the padding.
    """
    try:
        from PIL import Image
    except ImportError as exc:
        print(f"frame dump skipped: Pillow unavailable ({exc})", flush=True)
        return
    if fmt not in _PILLOW_RAW_MODES:
        print(f"frame dump skipped: no Pillow raw mode for {fmt!r}", flush=True)
        return
    # Read via the raw mode string so the padding byte is dropped, then convert:
    # PNG has no "RGBX" output mode, and ``copy()`` preserves the mode rather than
    # normalising it.  (That crashed the first run of this probe — after the
    # measurement had already completed, which is why the reading survived it.)
    image = Image.frombuffer("RGB", (width, height), raw, "raw", _PILLOW_RAW_MODES[fmt], 0, 1)
    image.convert("RGB").save(path)
    print(f"frame dumped to {path}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, help="media file to play")
    parser.add_argument(
        "--hwdec",
        default="drm-copy",
        help="hwdec mode; 'drm-copy' copies back to system RAM, which is what a "
        "software renderer needs. Pass 'no' to force software decoding.",
    )
    parser.add_argument("--size", default="1920x1200", help="WxH of the target buffer")
    parser.add_argument("--seconds", type=float, default=16.0, help="how long to draw for")
    parser.add_argument("--fps", type=int, default=30, help="target draw rate")
    parser.add_argument(
        "--format",
        dest="sw_format",
        default=SW_FORMAT,
        choices=sorted(SW_FORMATS),
        help="buffer format: rgb0/bgr0 are 4 bytes/pixel, rgb24 is 3, rgb565 is 2. "
        "This is the write-bandwidth cost reducer, and rgb565 matches the "
        "GL_RGB565 texture the production backend is meant to use.",
    )
    parser.add_argument(
        "--dump-frame",
        default="",
        help="write the last rendered frame here as PNG (needs Pillow) — the "
        "visual confirmation that the buffer holds a picture, not a blank",
    )
    args = parser.parse_args(argv)

    try:
        width, height = (int(part) for part in args.size.lower().split("x"))
    except ValueError:
        print(f"bad --size {args.size!r}; expected WxH", flush=True)
        return 1

    _install_sw_render_params()
    import mpv as mpvlib

    player = mpvlib.MPV(
        vo="libmpv",
        hwdec=args.hwdec,
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

    # One reused buffer, held for the whole run: the point is to measure what mpv
    # allocates per frame, so the host must not allocate per frame itself.  A
    # little slack is added so a scaler that rounds the row pitch up by a few
    # bytes cannot run off the end of the allocation.
    bpp = SW_FORMATS[args.sw_format]
    frame_size = width * height * bpp
    buffer = ctypes.create_string_buffer(frame_size + 4096)
    pointer = ctypes.c_void_p(ctypes.addressof(buffer))
    params = _render_params(width, height, pointer, args.sw_format)

    print(
        f"api_type=sw hwdec={args.hwdec} size={width}x{height} "
        f"format={args.sw_format} bytes_per_frame={frame_size}"
    )
    print(f"before play: fd={_fd_counts()[0]}", flush=True)

    player.play(args.video)
    time.sleep(WARMUP_SECONDS)

    total, sync = _fd_counts()
    print(f"post-warmup: fd={total:4d}  sync_file={sync:4d}", flush=True)

    # Fail loudly and distinguishably: "the software API does not work here" is a
    # legitimate answer to this spike, but it must not be confused with "it works
    # and is clean".
    try:
        ctx.render(**params)
    except Exception as exc:  # noqa: BLE001 - the failure IS the result
        print(f"RENDER FAILED: {type(exc).__name__}: {exc}", flush=True)
        return 2

    period = 1.0 / max(1, args.fps)
    deadline = time.monotonic()
    start = time.monotonic()
    # Process-wide CPU (all threads), so the decode thread is counted too.  The
    # software render path's real price is CPU, and the whole point of measuring
    # it here is that it is the trade-off being bought for the absence of fences.
    cpu_start = time.process_time()
    next_sample = start + SAMPLE_EVERY_SECONDS
    frames = 0
    samples: list[tuple[float, int, int, int, str, int]] = []

    while True:
        now = time.monotonic()
        if now - start >= args.seconds:
            break

        ctx.render(**params)
        ctx.report_swap()
        frames += 1

        if now >= next_sample:
            total, sync = _fd_counts()
            digest, lit = _fingerprint(bytes(buffer)[:frame_size])
            samples.append((now - start, frames, total, sync, digest, lit))
            print(
                f"t={now - start:5.1f}s frames={frames:5d}  fd={total:4d}  "
                f"sync_file={sync:4d}  pixels={digest} lit={lit}"
            )
            sys.stdout.flush()
            next_sample = now + SAMPLE_EVERY_SECONDS

        # Resynchronise rather than accumulate debt, matching the frontend's own
        # loop so the draw rate here is comparable to the measured 28-30/s.
        deadline += period
        sleep_for = deadline - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            deadline = time.monotonic()

    cpu_used = time.process_time() - cpu_start
    wall_used = time.monotonic() - start
    print()
    print(
        f"cpu              : {cpu_used:.2f}s over {wall_used:.2f}s wall "
        f"({cpu_used / wall_used * 100:.0f}% of one core, all threads)"
    )
    print(f"cpu per frame    : {cpu_used / max(1, frames) * 1000:.1f} ms")

    if args.dump_frame:
        _dump_frame(bytes(buffer)[:frame_size], width, height, args.dump_frame, args.sw_format)

    _report(player, samples, args.sw_format)

    ctx.free()
    player.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
