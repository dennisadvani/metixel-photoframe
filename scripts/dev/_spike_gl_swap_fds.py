#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""PHASE-0-style SPIKE — does presenting at 30fps leak descriptors?

Diagnostic only.  It answers a question the video work raised but cannot answer
itself: the frontend leaks ~1 DMA-BUF ``sync_file`` descriptor per presented video
frame (measured: ~24-30/s, matching the video's frame rate), and the leak is NOT
tied to hardware decoding — it happens with ``hwdec=no`` too.

That leaves two candidates: mpv's render into Qt's framebuffer, or Qt's own
frame-presentation path.  This probe removes mpv entirely and measures what each
shape of widget costs:

* ``--mode gl``             — a top-level ``QOpenGLWidget`` repainting at 30fps
* ``--mode raster``         — a top-level ``QWidget`` repainting at 30fps
* ``--mode nested_gl``      — a ``QOpenGLWidget`` CHILD inside a raster ``QWidget``
* ``--mode nested_raster``  — a ``QWidget`` child inside a raster ``QWidget``
* ``--mode gl_in_gl``       — a ``QOpenGLWidget`` child inside a GL ``QWidget``

The top-level modes came back CLEAN (514 frames, ``sync_file`` growth +0.0/s), which
rules out Qt's swap of a top-level window.  But production is not top-level: the
movie surface is a ``QOpenGLWidget`` nested inside ``FrameCanvas``, which is a
RASTER ``QWidget``.  Qt composites that child into the parent's backing store,
which is a different path and can allocate a fence per composite.  The ``nested_*``
modes reproduce that shape.

If a nested mode leaks while its top-level control does not, the leak is Qt
compositing the nested GL surface and has nothing to do with mpv.  If none leak,
the leak is inside mpv's render.

Descriptor counts are read from ``/proc/self/fd``, so the probe measures its own
process with no external tooling.

Usage:
    cage -d -- python3 _spike_gl_swap_fds.py --mode nested_gl
"""

from __future__ import annotations

import argparse
import os
import sys

#: How often to sample, and how many samples to take.
SAMPLE_EVERY_MS = 2000
SAMPLES = 7
#: First sample waits this long, so startup allocation is not counted as a leak.
WARMUP_MS = 3000
#: Repaint interval, ~30fps — the frontend's render-loop cap.
TICK_MS = 33

#: ``mode -> (root kind, child kind | None)``.  ``raster`` = plain ``QWidget``.
MODE_SPECS: dict[str, tuple[str, str | None]] = {
    "gl": ("gl", None),
    "raster": ("raster", None),
    "nested_gl": ("raster", "gl"),
    "nested_raster": ("raster", "raster"),
    "gl_in_gl": ("gl", "gl"),
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
    parser.add_argument("--mode", choices=sorted(MODE_SPECS), default="gl")
    args = parser.parse_args(argv)

    os.environ.setdefault("QT_QPA_PLATFORM", "wayland")

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication.instance() or QApplication([])

    frames = {"n": 0}

    def fill(widget: QWidget) -> None:
        """Repaint the whole surface, like the frame canvas does."""
        painter = QPainter(widget)
        try:
            # A varying colour, so the frame genuinely differs each tick and the
            # compositor cannot skip the swap.
            shade = 20 + (frames["n"] % 100)
            painter.fillRect(widget.rect(), QColor(shade, 40, 80))
        finally:
            painter.end()

    def surface_class(kind: str) -> type[QWidget]:
        """A widget class that repaints itself through :func:`fill`."""
        if kind == "gl":
            from PySide6.QtOpenGLWidgets import QOpenGLWidget

            class GlSurface(QOpenGLWidget):
                def paintGL(self) -> None:  # noqa: N802 - Qt naming
                    fill(self)

            return GlSurface

        class RasterSurface(QWidget):
            def paintEvent(self, _event: object) -> None:  # noqa: N802 - Qt naming
                fill(self)

        return RasterSurface

    root_kind, child_kind = MODE_SPECS[args.mode]
    root = surface_class(root_kind)()
    root.setWindowTitle(f"metixel fd probe [{args.mode}]")
    root.showFullScreen()

    # The window may not have its final size until it is mapped; the child rect is
    # derived from the screen so the inset is right on the first frame.
    screen = app.primaryScreen().geometry()
    width, height = screen.width(), screen.height()

    if child_kind is None:
        repainter: QWidget = root
    else:
        # Inset like the artwork rect inside the frame canvas, so the parent has to
        # composite the child over its own backing store rather than fill the window.
        inset = max(40, width // 6)
        repainter = surface_class(child_kind)(root)
        repainter.setGeometry(inset, inset, width - 2 * inset, height - 2 * inset)
        repainter.show()

    print(f"mode={args.mode} platform={app.platformName()}", flush=True)
    print(f"root={root_kind} child={child_kind} screen={width}x{height}", flush=True)

    def tick() -> None:
        frames["n"] += 1
        repainter.update()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(TICK_MS)

    samples: list[tuple[int, int, int]] = []

    def sample() -> None:
        total, sync = _fd_counts()
        samples.append((frames["n"], total, sync))
        print(f"frames={frames['n']:4d}  fd={total:4d}  sync_file={sync:4d}", flush=True)

    for index in range(SAMPLES):
        QTimer.singleShot(WARMUP_MS + index * SAMPLE_EVERY_MS, sample)
    QTimer.singleShot(WARMUP_MS + SAMPLES * SAMPLE_EVERY_MS + 300, app.quit)

    app.exec()

    if len(samples) >= 2:
        first, last = samples[0], samples[-1]
        grew = last[2] - first[2]
        per_second = grew / ((len(samples) - 1) * SAMPLE_EVERY_MS / 1000)
        print()
        print(f"frames presented : {frames['n']}")
        print(f"sync_file growth : {grew} ({per_second:+.1f}/s)")
        print(f"VERDICT [{args.mode}]:", "LEAKS" if grew > 10 else "clean")
        if grew > 10:
            print("  This shape leaks with NO mpv in the process — the cost is Qt")
            print("  presenting these widgets, not mpv rendering into them.")
        elif args.mode.startswith("nested"):
            print("  Nested compositing is clean, so the leak is inside mpv's render.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
