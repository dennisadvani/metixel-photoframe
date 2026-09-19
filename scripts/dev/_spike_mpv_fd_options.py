#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""SPIKE — which mpv option (if any) stops the per-frame descriptor leak?

The frontend leaks ~1 DMA-BUF ``sync_file`` descriptor per presented video frame
(~24-30/s, matching the frame rate) and dies of ``Too many open files`` after ~27s
of cumulative playback.  The leak has been localised by elimination:

* not metixel's own code — the per-tick ``setGeometry`` guard changed nothing;
* not hardware decoding — ``hwdec=no`` leaks too, with ``decode errors: 0``;
* not Qt — a bare ``QOpenGLWidget`` repainted at 30fps is clean, at top level AND
  nested inside a raster parent (the production shape), 541 frames, +0.0/s.

So the descriptor is created by mpv inside ``MpvRenderContext.render()``.  This
probe keeps everything else fixed and varies ONE mpv option, to find which part of
mpv's pipeline is responsible.

It deliberately subclasses the REAL :class:`metixel.display.qt_mpv.MpvRenderWidget`
and only overrides ``_create_mpv`` to inject extra options, so
``_init_render_context``, ``paintGL``, the update callback and the
``report_swap`` deferral are all production code.  A copy would be a different
program; this is the same one with one option poked.

SUPERSEDED for the finding, kept for the method: this probe was written against
the ``opengl`` render API, which is the path that leaks.  Production now uses
``api_type="sw"``, so the widget it subclasses no longer builds GL render
arguments and this sweep no longer reproduces the leak it was built to explain.

The variants are chosen to *discriminate between explanations*, not to guess:

* ``baseline`` — no extra option.  The control: it MUST leak, or this harness is
  not reproducing production and every other result is meaningless.
* ``swdecode`` — ``hwdec=no``.  A clean result would mean the leak needs hardware
  decode after all.
* ``dr_off`` — ``vd-lavc-dr=no``.  A clean result would put the leak in direct
  rendering, i.e. decoder buffers handed straight to GL.
* ``slow_frames`` — ``speed=0.1``.  A ~10x lower rate would mean the leak tracks
  FRAMES; an unchanged rate would mean it tracks wall-clock time.
* ``still_image`` — ``image-display-duration=inf``.  A clean result would mean the
  leak needs video decode at all, since this does one render and then holds.
* ``no_render`` — no extra option.  Repaints on every mpv frame callback but does
  NOT call ``ctx.render()``, so mpv still decodes while nothing is drawn.  This is
  the control that separates "mpv's draw call leaks" from "Qt receiving a repaint
  while an mpv context exists leaks".

Measured (Pi 5, cage, Mesa 26.2.1, mpv 0.40.0, all LEAKING):

    variant       sync_file growth      paints
    baseline      +30.1/s               78 -> 379
    swdecode      +26.1/s               70 -> 331
    dr_off        +28.6/s               75 -> 361
    slow_frames    +2.9/s               13 ->  42
    still_image    +0.0/s                5 ->   5

The leak is exactly ONE descriptor per ``ctx.render()`` call: ``sync_file ==
paints + 1`` in every leaking run.  ``hwdec=no`` and ``vd-lavc-dr=no`` both still
leak, so no decoder path is involved; ``speed=0.1`` cut the rate ~10x, so it
tracks DRAW CALLS rather than wall-clock time; and a still image, which draws only
5 times, did not leak at all.

``slow_frames`` is the sharpest of these: at 0.1x speed mpv produces ~10x fewer
frames in the same wall-clock window, so a per-frame leak drops ~10x while a
per-second (timer- or vsync-driven) leak does not.

``still_image`` plays the video's ``.1.frame`` poster instead of the video, which
exercises the same render call with a single decoded frame.

Usage (on the Pi, under cage, one variant per process):
    cage -d -- python3 _spike_mpv_fd_options.py --variant baseline \
        --video /opt/metixel/data/cache/videos/2cd3ac6ece47670f.mp4

One variant per PROCESS is required: descriptors are never returned, so a leaked
run poisons the baseline for the next one.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

#: Repaint/sample cadence.  Samples start after WARMUP_MS so startup allocation
#: (contexts, GL objects) is not mistaken for a leak.
SAMPLE_EVERY_MS = 2000
SAMPLES = 6
WARMUP_MS = 2500

#: mpv option name -> value, applied to the real widget's handle after creation.
VARIANTS: dict[str, dict[str, Any]] = {
    "baseline": {},
    "swdecode": {"hwdec": "no"},
    "dr_off": {"vd-lavc-dr": "no"},
    "slow_frames": {"speed": 0.1},
    "still_image": {"image-display-duration": "inf"},
    "no_render": {},
    # ``vo`` is set to ``libmpv`` by the real widget; these variants override it
    # before the render context is created (the injection happens in
    # ``_create_mpv``, which ``_init_gl`` calls just before MpvRenderContext).
    # ``gpu`` is the shader renderer and ``gpu-next`` is the libplacebo one, so
    # this asks whether the leak belongs to libplacebo rather than to mpv's
    # render API as a whole.
    "vo_gpu": {"vo": "gpu"},
    "vo_gpu_next": {"vo": "gpu-next"},
    # ``log_info`` prints mpv's own log so the active renderer is named by mpv
    # rather than inferred, and ``recycle`` tears the mpv handle down mid-run to
    # see whether that hands the leaked descriptors back (i.e. whether a
    # periodic recycle is a usable mitigation).
    "log_info": {"loglevel": "info"},
    "recycle": {},
    # These two CONSTRUCT mpv with a different ``vo`` (see VO_INIT_VARIANTS)
    # rather than poking it after the handle exists.  Setting ``vo`` post-hoc does
    # not switch renderer, it just stops the render API drawing at all, so the
    # earlier attempt produced a null result.
    "vo_init_gpu": {},
    "vo_init_gpu_next": {},
}

#: Variants that must be built with a different ``vo`` FROM THE START.  The real
#: widget hardcodes ``vo="libmpv"`` (the render API requires it), so this is the
#: only way to ask whether the render API can drive the non-libplacebo renderer.
VO_INIT_VARIANTS: dict[str, str] = {
    "vo_init_gpu": "gpu",
    "vo_init_gpu_next": "gpu-next",
}


def _fd_counts() -> tuple[int, int]:
    """Return ``(total_fds, sync_file_fds)`` for THIS process."""
    total = 0
    sync = 0
    for name in os.listdir("/proc/self/fd"):
        total += 1
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue  # closed under us — normal for a transient fd
        if "sync_file" in target:
            sync += 1
    return total, sync


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=sorted(VARIANTS), required=True)
    parser.add_argument("--video", required=True, help="media file to play")
    args = parser.parse_args(argv)

    os.environ.setdefault("QT_QPA_PLATFORM", "wayland")

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    from metixel.display.qt_mpv import MpvRenderWidget

    options = dict(VARIANTS[args.variant])
    # ``hwdec`` is a constructor argument in the real widget; the rest are applied
    # to the live handle.
    hwdec = str(options.pop("hwdec", "auto"))

    class ProbeWidget(MpvRenderWidget):
        """The production widget, with a few extra options poked in."""

        def __init__(self) -> None:
            super().__init__(None, hwdec=hwdec)
            self.paints = 0
            self.rejected: list[str] = []
            # ``no_render``: repaint on mpv's frame callback but never draw, so
            # decoding continues while the render call is skipped entirely.
            self.skip_render = args.variant == "no_render"
            self.verbose = args.variant == "log_info"
            self.vo_init = VO_INIT_VARIANTS.get(args.variant)

        def _mpv_log(self, level: str, component: str, message: str) -> None:
            """Surface mpv's own log verbatim for ``log_info``.

            Which renderer the render API picks is mpv's business; reading it out
            of mpv's log is stronger evidence than inferring it from behaviour.
            """
            if self.verbose:
                print(f"  [mpv:{component}] {message.strip()}", flush=True)
            else:
                super()._mpv_log(level, component, message)

        def _create_mpv(self) -> bool:
            if self.vo_init is None:
                if not super()._create_mpv():
                    return False
                for name, value in options.items():
                    try:
                        self._mpv[name] = value
                    except Exception as exc:  # noqa: BLE001 - a failed option must be LOUD
                        self.rejected.append(f"{name}={value} ({exc})")
                return True

            # Built from scratch so ``vo`` is set before the handle initialises.
            # This mirrors the real widget's kwargs exactly, except for ``vo``.
            import mpv as mpvlib

            self._mpv = mpvlib.MPV(
                vo=self.vo_init,
                hwdec=self._hwdec,
                mute=True,
                loop=False,
                keep_open="no",
                osc=False,
                osd_level=0,
                input_default_bindings=False,
                input_vo_keyboard=False,
                loglevel="warn",
                log_handler=self._mpv_log,
            )
            self._mpv.observe_property("eof-reached", self._on_eof_reached)
            self._mpv.observe_property("video-params", self._on_video_params)
            self._mpv.observe_property("pause", self._on_pause_changed)
            return True

        def paintGL(self) -> None:  # noqa: N802 - Qt naming
            self.paints += 1
            if not self.skip_render:
                super().paintGL()

    widget = ProbeWidget()
    widget.setWindowTitle(f"metixel mpv fd probe [{args.variant}]")
    widget.showFullScreen()

    print(f"variant={args.variant} options={options or '{}'} hwdec={hwdec}", flush=True)
    print(f"platform={app.platformName()} size={widget.width()}x{widget.height()}", flush=True)

    widget.ensure_render_context()
    if widget.rejected:
        for entry in widget.rejected:
            print(f"  OPTION REJECTED: {entry}", flush=True)

    before_total, before_sync = _fd_counts()
    print(f"pre-play  fd={before_total}  sync_file={before_sync}", flush=True)

    widget.play(args.video)

    samples: list[tuple[int, int, int]] = []

    def sample() -> None:
        total, sync = _fd_counts()
        samples.append((widget.paints, total, sync))
        print(f"paints={widget.paints:4d}  fd={total:4d}  sync_file={sync:4d}", flush=True)

    for index in range(SAMPLES):
        QTimer.singleShot(WARMUP_MS + index * SAMPLE_EVERY_MS, sample)

    if args.variant == "recycle":

        def do_recycle() -> None:
            """Tear the mpv handle down mid-run and see if the fds come back."""
            total, sync = _fd_counts()
            print(f"  pre-recycle   fd={total} sync_file={sync}", flush=True)
            widget.destroy_mpv()
            total, sync = _fd_counts()
            print(f"  post-destroy  fd={total} sync_file={sync}", flush=True)
            widget.ensure_render_context()
            widget.play(args.video)
            total, sync = _fd_counts()
            print(f"  post-restart  fd={total} sync_file={sync}", flush=True)

        QTimer.singleShot(WARMUP_MS + 3 * SAMPLE_EVERY_MS, do_recycle)

    def finish() -> None:
        # Which path did mpv actually take?  Without this, ``swdecode`` leaking
        # could be misread as "software decode leaks" when hardware decode had
        # silently refused to engage.
        props = {}
        for name in ("hwdec-current", "vd-lavc-dr", "video-params/pixelformat", "width"):
            try:
                props[name] = widget._mpv[name]
            except Exception:  # noqa: BLE001 - diagnostics only
                props[name] = "?"
        print(f"mpv props: {props}", flush=True)
        print(f"rendered paints: {widget.paints}", flush=True)
        app.quit()

    QTimer.singleShot(WARMUP_MS + SAMPLES * SAMPLE_EVERY_MS + 300, finish)

    app.exec()

    if len(samples) >= 2:
        first, last = samples[0], samples[-1]
        grew = last[2] - first[2]
        seconds = (len(samples) - 1) * SAMPLE_EVERY_MS / 1000
        print()
        print(f"sync_file growth : {grew} ({grew / seconds:+.1f}/s)")
        print(f"VERDICT [{args.variant}]:", "LEAKS" if grew > 10 else "clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
