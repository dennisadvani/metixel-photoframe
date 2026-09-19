#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""PHASE-0 SPIKE — does a painted *partial* hole over a sibling widget work?

This is a throwaway diagnostic, not shipped code.  It answers one question
before any production video work starts:

    Can a widget on top paint everything EXCEPT a rectangle (the "artwork
    hole"), so a sibling widget underneath shows through that rectangle —
    under cage, on this Pi — or does the unpainted region render black?

Why it has to be asked
----------------------
``FrameCanvas.set_overlay_only`` already proves the *fully* transparent case
works: it clears the widget and the surface below shows through.  The planned
video architecture needs the *partial* case — the canvas keeps painting the
ambient fill, the rings and the overlay, leaving only the artwork rectangle
transparent so the video surface shows through exactly there.

``qt_canvas.py`` records an earlier attempt at a transparent hole that rendered
as "a solid black rectangle", which is why the current design moved the matte
*into* the mpv widget instead.  Before committing to a design that reintroduces
a hole, we need to know whether that failure was the ``WA_OpaquePaintEvent``
contract (fixable, and already fixed by ``set_overlay_only``) or something
structural about a GL sibling.  The cases below separate those.

The matrix
----------
Each case is a separate process: a GL context and a widget tree do not survive
reconfiguration cleanly.

``solid``      hole over a plain raster ``QWidget``     — Qt's own compositor
               handling of a partial hole.
``glwidget``   hole over a plain ``QOpenGLWidget``      — whether a GL sibling
               specifically breaks it.
``mpv_idle``   hole over the real ``MpvRenderWidget``,  — mpv's FBO alpha/clear
               before playback.                            state.
``mpv_play``   hole over the real ``MpvRenderWidget``,  — whether the result
               playing, cropped to fill (``panscan=1.0``).  changes once frames
                                                           arrive.

Capture is by ``grim``, not ``QWidget.grab()``: grim asks the compositor for the
composited output over ``wlr-screencopy``, so it captures what is actually on
the panel, GL surfaces included.  A widget grab cannot see the mpv surface and
would therefore miss exactly the thing under test.

Each capture is classified into a *symptom*:

* ``hole_shows_underlay`` — the mechanism works (the pass condition).
* ``hole_black``          — the documented failure: a partial hole is not
                            punched, so the framebuffer shows through black.
* ``overlay_behind``      — z-order is wrong; the underlay is on top.
* ``hole_not_punched``    — the overlay painted its full rect, no hole at all.
* ``indeterminate``       — none of the known signatures matched.

Run it with ``_spike_video_hole.sh``, which stops ``metixel-cage`` so this can
own the display and restores it afterwards.

Usage:
    cage -- python3 _spike_video_hole.py --case solid --outdir /tmp/spike
"""

from __future__ import annotations

import argparse
import json
import locale
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# -- Fixed test geometry ----------------------------------------------------

#: Colour painted by the OVERLAY everywhere except the hole.  Saturated, and
#: unlikely to appear in a photo, so a match is unambiguous.
OVERLAY_COLOUR = (0, 170, 0)

#: Colour painted by the raster/GL UNDERLAY (the sibling that must show
#: through).  Pure magenta: never black, never green, never in the video.
UNDERLAY_COLOUR = (255, 0, 255)

#: Per-channel tolerance for a colour match.  Absorbs a compositor's colour
#: rounding while still telling magenta from black.
TOLERANCE = 24

#: Stricter threshold for "this pixel is black" — i.e. uninitialised framebuffer
#: rather than merely a dark picture.  Deliberately much tighter than
#: :data:`TOLERANCE`: a night scene in a video legitimately samples near-zero,
#: and judging that as "black hole" would report a working hole as a failure.
BLACK_TOLERANCE = 8

#: Hole size as a fraction of the screen, capped in pixels.  Recomputed from
#: the captured frame so the analyser and the widget agree even if the
#: compositor hands back a different size than requested.
HOLE_FRACTION = (0.40, 0.45)
HOLE_MAX = (760, 560)

#: Fallback video.  A 1920x1080 H.264 sample already on the frame, so
#: ``panscan=1.0`` genuinely crops.
DEFAULT_VIDEO = "/opt/metixel/data/media/sample_media/landscape/13131508_1920_1080_24fps.mp4"

#: Settle time before capturing, and how long to let mpv produce frames.
SETTLE_S = 1.5
PLAY_S = 2.5

#: grim lives here (see ``metixel.display.screenshot``).
GRIM = "/usr/bin/grim"


def _hole_rect(width: int, height: int) -> tuple[int, int, int, int]:
    """Return the hole as ``(x, y, w, h)``, centred and capped."""
    w = min(HOLE_MAX[0], max(120, int(width * HOLE_FRACTION[0])))
    h = min(HOLE_MAX[1], max(120, int(height * HOLE_FRACTION[1])))
    return ((width - w) // 2, (height - h) // 2, w, h)


def _near(actual: tuple[int, int, int], expected: tuple[int, int, int]) -> bool:
    """Whether *actual* matches *expected* within :data:`TOLERANCE`."""
    return all(abs(a - b) <= TOLERANCE for a, b in zip(actual, expected, strict=True))


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


def _fill_widget(colour: tuple[int, int, int]) -> Any:
    """A raster sibling that paints a plain colour — the ``solid`` underlay."""
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import QWidget

    qt_colour = QColor(*colour)

    class _Fill(QWidget):
        def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt naming
            painter = QPainter(self)
            try:
                painter.fillRect(self.rect(), qt_colour)
            finally:
                painter.end()

    return _Fill()


def _gl_fill_widget(colour: tuple[int, int, int]) -> Any:
    """A ``QOpenGLWidget`` painting a plain colour — the ``glwidget`` underlay."""
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtOpenGLWidgets import QOpenGLWidget

    qt_colour = QColor(*colour)

    class _GlFill(QOpenGLWidget):
        def paintGL(self) -> None:  # noqa: N802 - Qt naming
            painter = QPainter(self)
            try:
                painter.fillRect(self.rect(), qt_colour)
            finally:
                painter.end()

    return _GlFill()


def _hole_overlay() -> Any:
    """An overlay that paints its whole rect EXCEPT the hole.

    The load-bearing detail: ``WA_OpaquePaintEvent`` is deliberately NOT set.
    That flag is a contract meaning "I paint every pixel", and claiming it while
    leaving the hole unpainted is exactly what showed uninitialised framebuffer
    (black) in the earlier attempt.  Leaving it clear lets the unpainted region
    fall through to the sibling underneath.
    """
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor, QPainter, QRegion
    from PySide6.QtWidgets import QWidget

    qt_colour = QColor(*OVERLAY_COLOUR)

    class _HoleOverlay(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.hole = QRect(0, 0, 0, 0)

        def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt naming
            painter = QPainter(self)
            try:
                clip = QRegion(self.rect()).subtracted(QRegion(self.hole))
                painter.setClipRegion(clip)
                painter.fillRect(self.rect(), qt_colour)
            finally:
                painter.end()

    return _HoleOverlay()


# ---------------------------------------------------------------------------
# Capture + analysis
# ---------------------------------------------------------------------------


def _capture(path: Path) -> tuple[bool, str]:
    """Ask the compositor for the composited frame via grim."""
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    try:
        proc = subprocess.run(
            [GRIM, str(path)],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or "").strip() or f"grim exited {proc.returncode}"
    return True, ""


def _analyse(
    png: Path, case: str, hole: tuple[int, int, int, int], window: tuple[int, int]
) -> dict[str, Any]:
    """Sample the capture and classify the symptom.

    *hole* and *window* come from the widget itself rather than being recomputed
    from the capture.  They must be, because cage sizes its surface to the
    bounding box of every enabled output, so a phantom HDMI port makes the
    captured frame wider than the window.  Recomputing from the capture would
    then sample the wrong place and invent a failure.
    """
    from PIL import Image, ImageStat

    img = Image.open(png).convert("RGB")
    width, height = img.size
    hx, hy, hw, hh = hole

    def px(x: int, y: int) -> tuple[int, int, int]:
        x = max(0, min(width - 1, x))
        y = max(0, min(height - 1, y))
        return tuple(img.getpixel((x, y))[:3])  # type: ignore[return-value]

    inset = 12
    hole_points = [
        (hx + inset, hy + inset),
        (hx + hw - inset, hy + inset),
        (hx + inset, hy + hh - inset),
        (hx + hw - inset, hy + hh - inset),
        (hx + hw // 2, hy + hh // 2),
    ]
    # Just OUTSIDE the hole on each side, plus the four screen corners.
    gap = 8
    surround_points = [
        (hx + hw // 2, max(0, hy - gap)),
        (hx + hw // 2, min(height - 1, hy + hh + gap)),
        (max(0, hx - gap), hy + hh // 2),
        (min(width - 1, hx + hw + gap), hy + hh // 2),
        (4, 4),
        (width - 5, 4),
        (4, height - 5),
        (width - 5, height - 5),
    ]

    hole_samples = [px(*p) for p in hole_points]
    surround_samples = [px(*p) for p in surround_points]

    # Standard deviation INSIDE the hole distinguishes video content from a flat
    # fill: a photo/video is high-variance, black or a solid colour is not.
    hole_crop = img.crop((hx + inset, hy + inset, hx + hw - inset, hy + hh - inset))
    stats = ImageStat.Stat(hole_crop)
    spread = max(stats.stddev) if stats.stddev else 0.0

    underlay_hits = sum(1 for c in hole_samples if _near(c, UNDERLAY_COLOUR))
    black_hits = sum(1 for c in hole_samples if max(c) <= BLACK_TOLERANCE)
    overlay_hits = sum(1 for c in hole_samples if _near(c, OVERLAY_COLOUR))
    surround_ok = sum(1 for c in surround_samples if _near(c, OVERLAY_COLOUR))

    total = len(hole_samples)
    n_surround = len(surround_samples)

    # ORDER matters: "covered by the overlay" and "wrong z-order" are more
    # specific diagnoses than "black", and a black hole is only the real finding
    # once the overlay provably has a hole and is on top.
    if overlay_hits >= total - 1:
        symptom = "hole_not_punched"
    elif surround_ok < n_surround // 2:
        symptom = "overlay_behind"
    elif black_hits >= total - 1:
        symptom = "hole_black"
    elif case in ("solid", "glwidget"):
        symptom = "hole_shows_underlay" if underlay_hits >= total - 1 else "indeterminate"
    else:
        # mpv: the hole is showing the live surface when it is neither black nor
        # the overlay's colour.  Deliberately independent of variance — a dark or
        # frozen video frame is legitimately low-variance, so keying on it would
        # misreport a working hole as indeterminate.
        symptom = "hole_shows_underlay" if overlay_hits == 0 else "indeterminate"

    return {
        "screen": [width, height],
        "capture_matches_window": [width, height] == list(window),
        "hole": [hx, hy, hw, hh],
        "hole_samples": hole_samples,
        "surround_samples": surround_samples,
        "hole_underlay_hits": f"{underlay_hits}/{total}",
        "hole_black_hits": f"{black_hits}/{total}",
        "hole_overlay_hits": f"{overlay_hits}/{total}",
        "surround_overlay_hits": f"{surround_ok}/{n_surround}",
        "hole_stddev": round(float(spread), 2),
        "symptom": symptom,
        "expected": _EXPECTATION[case],
        "verdict_ok": _EXPECTATION[case] is None or symptom == _EXPECTATION[case],
    }


#: What a PASS looks like for each case.  ``None`` means informational: the case
#: cannot pass or fail, it only tells us something.
#:
#: ``mpv_idle`` is informational because a black hole there is EXPECTED and
#: harmless: ``MpvRenderWidget`` sets ``WA_OpaquePaintEvent`` (it claims to paint
#: every pixel) but its ``paintGL`` returns early until a render context has
#: produced a frame, so before playback there is genuinely nothing to show.  The
#: useful conclusion is the opposite one — the hole must not be revealed until
#: the first frame is ready, which is why ``video_ready()`` is in the plan.
_EXPECTATION: dict[str, str | None] = {
    "solid": "hole_shows_underlay",
    "glwidget": "hole_shows_underlay",
    "mpv_idle": None,
    "mpv_play": "hole_shows_underlay",
}


_SYMPTOM_MEANING = {
    "hole_shows_underlay": "PASS — the sibling shows through the hole",
    "hole_black": "FAIL — unpainted hole renders black (the documented failure)",
    "overlay_behind": "FAIL — z-order inverted; the underlay is on top",
    "hole_not_punched": "FAIL — overlay painted its full rect (no hole)",
    "indeterminate": "UNCLEAR — none of the known signatures matched",
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _run(case: str, outdir: Path, video: str) -> int:
    from PySide6.QtCore import QRect, QTimer, qVersion
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication.instance() or QApplication([])
    # mpv requires the C numeric locale, and Qt resets it in its constructor.
    locale.setlocale(locale.LC_NUMERIC, "C")

    container = QWidget()
    container.setWindowTitle(f"metixel phase-0 spike [{case}]")
    container.setStyleSheet("background-color: black;")

    if case == "solid":
        underlay = _fill_widget(UNDERLAY_COLOUR)
    elif case == "glwidget":
        underlay = _gl_fill_widget(UNDERLAY_COLOUR)
    elif case in ("mpv_idle", "mpv_play"):
        from metixel.display.qt_mpv import MpvRenderWidget
        from metixel.shared.platform import detect_pi_model, hwdec_for_model

        underlay = MpvRenderWidget(hwdec=hwdec_for_model(detect_pi_model()))
    else:
        raise SystemExit(f"unknown case {case!r}")

    overlay = _hole_overlay()
    underlay.setParent(container)
    overlay.setParent(container)
    container.showFullScreen()

    # The surface is not necessarily at its final size when showFullScreen()
    # returns (the compositor configures it asynchronously).  _sync_geometry is
    # therefore called again from the settle timer, and the analysed hole is
    # recomputed from the captured frame's dimensions anyway.
    def sync_geometry() -> None:
        rect = container.rect()
        underlay.setGeometry(rect)
        overlay.setGeometry(rect)
        overlay.hole = QRect(*_hole_rect(rect.width(), rect.height()))
        overlay.raise_()

    sync_geometry()

    started: dict[str, str | None] = {"play": None}

    def start_play() -> None:
        if case != "mpv_play":
            return
        try:
            underlay.ensure_render_context()
            playback = underlay._mpv  # spike: reaching in is the point
            if playback is not None:
                # Fill (cover) the widget so the hole shows video, not mpv's own
                # black letterbox bars — which look exactly like the failure
                # this spike is hunting for.
                playback.panscan = 1.0
            underlay.play(video)
            started["play"] = "ok"
        except Exception as exc:  # a spike must always report, never crash
            started["play"] = f"{type(exc).__name__}: {exc}"

    report: dict[str, Any] = {
        "case": case,
        "qt": qVersion(),
        "platform_plugin": QGuiApplication.platformName(),
        "env": {
            "QT_QPA_PLATFORM": os.environ.get("QT_QPA_PLATFORM", ""),
            "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY", ""),
            "XDG_SESSION_TYPE": os.environ.get("XDG_SESSION_TYPE", ""),
        },
        "video": video if case == "mpv_play" else None,
    }

    def finish() -> None:
        report["play_call"] = started["play"]
        png = outdir / f"{case}.png"
        ok, err = _capture(png)
        report["capture_ok"] = ok
        report["capture_error"] = err
        report["png"] = str(png)
        window = (container.width(), container.height())
        hole = (
            overlay.hole.x(),
            overlay.hole.y(),
            overlay.hole.width(),
            overlay.hole.height(),
        )
        report["window"] = list(window)
        report["overlay_hole"] = list(hole)
        if ok:
            report.update(_analyse(png, case, hole, window))
        app.quit()

    # Schedule deliberately sequenced: settle the geometry, (optionally) start
    # playback, let frames arrive, then capture.  sync_geometry runs twice
    # because the compositor configures the surface asynchronously, so the first
    # call may see a placeholder size.
    QTimer.singleShot(300, sync_geometry)
    QTimer.singleShot(900, sync_geometry)
    if case == "mpv_play":
        QTimer.singleShot(900, start_play)
        QTimer.singleShot(int((SETTLE_S + PLAY_S) * 1000), finish)
    else:
        QTimer.singleShot(int(SETTLE_S * 1000), finish)

    app.exec()

    (outdir / f"{case}.json").write_text(json.dumps(report, indent=2))

    print()
    print(f"=== case: {case} ===")
    print(f"  platform plugin  : {report['platform_plugin'] or '(default)'}")
    print(f"  window           : {report.get('window')}")
    print(f"  hole (analysed)  : {report.get('hole')}")
    print(f"  play() call      : {report['play_call']}")
    if not report.get("capture_ok"):
        print(f"  CAPTURE FAILED   : {report.get('capture_error')}")
        return 2
    print(f"  hole samples     : {report['hole_samples']}")
    print(f"  surround samples : {report['surround_samples']}")
    print(
        "  hole counts      : "
        f"underlay={report['hole_underlay_hits']} "
        f"black={report['hole_black_hits']} "
        f"overlay={report['hole_overlay_hits']}"
    )
    print(
        f"  surround overlay : {report['surround_overlay_hits']}  "
        f"hole stddev={report['hole_stddev']}"
    )
    print(
        f"  SYMPTOM          : {report['symptom']} — {_SYMPTOM_MEANING.get(report['symptom'], '?')}"
    )
    expected = report.get("expected")
    if expected is None:
        print("  EXPECTED         : (informational — no pass/fail for this case)")
    else:
        print(
            f"  EXPECTED         : {expected}  →  {'PASS' if report.get('verdict_ok') else 'FAIL'}"
        )
    if not report.get("capture_matches_window", True):
        print(
            "  NOTE             : capture size != window size — a phantom output "
            "widened cage's surface (see scripts/cage_launch.sh)"
        )

    # Always exit 0: this is a diagnostic, and the caller wants the report and
    # the PNG even when — especially when — the answer is "it fails".
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", required=True, choices=["solid", "glwidget", "mpv_idle", "mpv_play"]
    )
    parser.add_argument("--outdir", default="/tmp/spike")
    parser.add_argument("--video", default=DEFAULT_VIDEO)
    args = parser.parse_args(argv)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    return _run(args.case, outdir, args.video)


if __name__ == "__main__":
    sys.exit(main())
