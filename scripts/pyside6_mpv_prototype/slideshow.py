#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""PySide6 + mpv slideshow prototype (cage/Wayland, python-mpv render API).

A standalone image + video slideshow that combines PySide6 (Qt6) for the
window/UI and mpv for video playback, to test performance and capabilities
on a Raspberry Pi.

This version runs under **cage** (Wayland compositor) and embeds mpv video
via **python-mpv's MpvRenderContext** (the libmpv OpenGL render API),
compositing mpv's output as a texture inside a QOpenGLWidget. This is the
same approach used by booru-viewer and works natively on Wayland (no
XWayland needed).

Usage:
    cage -- python scripts/pyside6_mpv_prototype/slideshow.py \
        --media data/media/sample_media
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import locale
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPixmap
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QLabel,
    QMainWindow,
    QVBoxLayout,
    QWidget,
)

import mpv as mpvlib

# ── Defaults ──────────────────────────────────────────────────────────────

DEFAULT_MEDIA = Path(__file__).resolve().parent.parent.parent / "data" / "media" / "sample_media"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}


# ══════════════════════════════════════════════════════════════════════════
#  System stats (Linux /proc)
# ══════════════════════════════════════════════════════════════════════════

def _read_cpu_times() -> list[int]:
    try:
        with open("/proc/stat", encoding="utf-8") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts = line.split()
                    return [int(x) for x in parts[1:]]
    except OSError:
        pass
    return []


def _cpu_percent(prev: list[int], now: list[int]) -> float:
    if not prev or not now or len(prev) < 4 or len(now) < 4:
        return 0.0
    prev_idle = prev[3] + (prev[4] if len(prev) > 4 else 0)
    now_idle = now[3] + (now[4] if len(now) > 4 else 0)
    d_total = sum(now) - sum(prev)
    d_idle = now_idle - prev_idle
    if d_total <= 0:
        return 0.0
    return 100.0 * (1.0 - d_idle / d_total)


def _mem_info() -> dict:
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable", "MemFree"):
                    out[key] = int(rest.strip().split()[0])
    except OSError:
        pass
    return out


# ══════════════════════════════════════════════════════════════════════════
#  Media widgets
# ══════════════════════════════════════════════════════════════════════════

class ImageWidget(QWidget):
    """Renders an image with a virtual matte (object-fit: contain).

    The matte is blue and the image is centred, taking up 70% of the screen.
    """

    # Matte colour (blue) and the fraction of the screen the image occupies.
    MATTE_COLOR = QColor(0, 0, 255)
    FIT_FRACTION = 0.70

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._fit_rect: tuple[int, int, int, int] | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    def set_image(self, path: Path) -> None:
        self._pixmap = QPixmap(str(path))
        self._fit_rect = self._compute_fit_rect(self._pixmap.size()) if not self._pixmap.isNull() else None
        self.update()

    def clear(self) -> None:
        self._pixmap = None
        self._fit_rect = None
        self.update()

    def _compute_fit_rect(self, size) -> tuple[int, int, int, int]:
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0 or size.width() <= 0 or size.height() <= 0:
            return (0, 0, w, h)
        # Fit the image within 70% of the screen, preserving aspect ratio.
        avail_w = int(w * self.FIT_FRACTION)
        avail_h = int(h * self.FIT_FRACTION)
        scale = min(avail_w / size.width(), avail_h / size.height())
        dw, dh = int(size.width() * scale), int(size.height() * scale)
        return ((w - dw) // 2, (h - dh) // 2, dw, dh)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.MATTE_COLOR)
        if self._pixmap and self._fit_rect:
            x, y, dw, dh = self._fit_rect
            painter.drawPixmap(x, y, dw, dh, self._pixmap)
        painter.end()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._pixmap:
            self._fit_rect = self._compute_fit_rect(self._pixmap.size())
            self.update()


class MpvRenderWidget(QOpenGLWidget):
    """QOpenGLWidget that composites mpv video via python-mpv's render API.

    Mirrors booru-viewer's mpv_gl.py: creates the mpv instance eagerly,
    then on first GL init creates a MpvRenderContext bound to the current
    QOpenGLContext, and renders mpv frames into the widget on each paint.
    Works natively on Wayland (cage).
    """

    _frame_ready = Signal()  # mpv thread -> main thread repaint trigger

    def __init__(self, parent: QWidget | None = None, hwdec: str = "auto",
                 fast: bool = False, display_fps: float = 60.0) -> None:
        super().__init__(parent)
        self._mpv: mpvlib.MPV | None = None
        self._ctx: mpvlib.MpvRenderContext | None = None
        self._proc_addr_fn = None
        self._gl_inited = False
        self._on_eof = None
        self._on_fps = None
        self._frame_ready.connect(self.update)

        self._video_size: tuple[int, int] | None = None

        # Create mpv eagerly on the main thread (after LC_NUMERIC fix).
        opts: dict = {
            "vo": "libmpv",
            "hwdec": hwdec,
            "mute": True,
            "loop": False,
            "keep_open": "no",
            "osc": False,
            "osd_level": 0,  # disable mpv's OSD (on-screen text baked into the frame)
            "input_default_bindings": False,
            "input_vo_keyboard": False,
            "loglevel": "debug",
            "log_handler": self._mpv_log,
        }
        if fast:
            # Aggressive low-power optimisations for weak GPUs (Pi 3 VC4).
            # The default spline36 scaler + ordered dithering are very
            # expensive on the VC4. These trade a little quality for a lot
            # of GPU headroom.
            opts.update({
                "scale": "bilinear",        # cheap upscale (default spline36 is costly)
                "dscale": "bilinear",       # cheap downscale
                "cscale": "bilinear",       # cheap chroma upscale
                "dither-depth": "no",       # disable dithering entirely
                "correct-downscaling": False,
                "linear-downscaling": False,
                "video-unscaled": False,
                "vd-lavc-threads": 2,       # cap decode threads (4-core Pi 3)
                "audio": False,             # no audio needed for a photo frame
                "ao": "null",               # don't touch the sound card at all
                "demuxer-readahead-secs": 2,
                "cache": "no",
                "vd-lavc-fast": True,       # skip some decoder checks
                "vd-lavc-skiploopfilter": "nonref",  # skip loop filter on non-ref frames
                "deband": False,
                "hdr-compute-peak": False,
                "target-colorspace-hint": False,
                "video-output-levels": "auto",
                "gpu-api": "opengl",
                "opengl-rectangle-textures": False,
                "opengl-pbo": True,
                # Pace video to the display refresh rate. Without this, mpv
                # renders as fast as it can and the VC4 (no swap control)
                # delivers frames unevenly -> choppy playback.
                "video-sync": "display-resample",
                # The libmpv render API doesn't auto-detect the display
                # refresh rate (no swap control on VC4). Tell mpv explicitly
                # so display-resample can pace frames to the real refresh.
                "display-fps-override": display_fps,
            })
        self._mpv = mpvlib.MPV(**opts)
        self._mpv.observe_property("eof-reached", self._on_eof_reached)
        self._mpv.observe_property("estimated-vf-fps", self._on_fps_changed)
        self._mpv.observe_property("video-params", self._on_video_params)

    def _mpv_log(self, level: str, component: str, message: str) -> None:
        if level in ("error", "warn", "info", "v"):
            print(f"[mpv:{component}] {message.strip()}")

    # -- mpv callbacks (called from mpv thread) --

    def _on_eof_reached(self, _name: str, value) -> None:
        if value and self._on_eof:
            self._on_eof()

    def _on_fps_changed(self, _name: str, value) -> None:
        if value and value > 0 and self._on_fps:
            self._on_fps(value)

    def _on_video_params(self, _name: str, value) -> None:
        if isinstance(value, dict) and value.get("w") and value.get("h"):
            self._video_size = (value["w"], value["h"])

    # -- Public API --

    def set_callbacks(self, on_eof, on_fps) -> None:
        self._on_eof = on_eof
        self._on_fps = on_fps

    def play(self, path: str) -> None:
        if self._mpv:
            self._mpv.play(path)

    def stop(self) -> None:
        if self._mpv:
            self._mpv.command("stop")

    def destroy_mpv(self) -> None:
        if self._ctx:
            self._ctx.free()
            self._ctx = None
        if self._mpv:
            self._mpv.terminate()
            self._mpv = None

    # -- GL lifecycle ------------------------------------------------------

    def initializeGL(self) -> None:  # noqa: N802
        self._init_gl()

    def _init_gl(self) -> None:
        if self._gl_inited or self._mpv is None:
            return
        from PySide6.QtGui import QOpenGLContext
        ctx = QOpenGLContext.currentContext()
        if not ctx:
            return

        def _get_proc_address(_ctx, name):
            if isinstance(name, bytes):
                name_str = name
            else:
                name_str = name.encode("utf-8")
            addr = ctx.getProcAddress(name_str)
            if addr is not None:
                return int(addr)
            return 0

        self._proc_addr_fn = mpvlib.MpvGlGetProcAddressFn(_get_proc_address)
        self._ctx = mpvlib.MpvRenderContext(
            self._mpv, "opengl",
            opengl_init_params={"get_proc_address": self._proc_addr_fn},
        )
        self._ctx.update_cb = self._on_mpv_frame
        self._gl_inited = True
        print("[mpv] render context initialized")

    def ensure_gl_init(self) -> None:
        """Force GL context creation and render context setup.

        Needed when the widget is hidden (e.g. inside a QStackedWidget)
        but mpv needs a render context before loadfile().
        """
        if not self._gl_inited:
            self.makeCurrent()
            self._init_gl()

    def _on_mpv_frame(self) -> None:
        """Called from mpv thread when a new frame is ready."""
        self._frame_ready.emit()

    def paintGL(self) -> None:  # noqa: N802
        if not self._ctx:
            return
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return

        # Compute the fitted video rect: 70% of the widget, centred,
        # preserving the video's aspect ratio (object-fit: contain).
        vw, vh = self._video_size if self._video_size else (w, h)
        avail_w = int(w * ImageWidget.FIT_FRACTION)
        avail_h = int(h * ImageWidget.FIT_FRACTION)
        scale = min(avail_w / vw, avail_h / vh)
        dw, dh = int(vw * scale), int(vh * scale)
        dx, dy = (w - dw) // 2, (h - dh) // 2

        # Render mpv directly into the widget's default FBO at full size.
        # mpv letterboxes the video itself (object-fit: contain) within the
        # full widget, so the video is centred and aspect-correct.
        self._ctx.render(
            opengl_fbo={
                "fbo": self.defaultFramebufferObject(),
                "w": w,
                "h": h,
                "internal_format": 0,
            },
            flip_y=True,
        )
        # CRITICAL: report_swap() must be called AFTER the buffer is actually
        # presented to the display, not immediately after render(). The swap
        # happens after paintGL() returns (Qt swaps automatically). Calling it
        # too early makes mpv think frames are presented sooner than they are,
        # which breaks frame pacing — especially on GPUs without swap control
        # (VC4: "GL_*_swap_control extension missing"). Defer it to the next
        # event-loop iteration, which runs after the swap.
        QTimer.singleShot(0, self._ctx.report_swap)

        # Paint the blue matte border over the video, leaving only the
        # centred 70% rect showing the video. This avoids the fragile
        # FBO-blit approach entirely.
        painter = QPainter(self)
        painter.fillRect(0, 0, w, dy, ImageWidget.MATTE_COLOR)          # top
        painter.fillRect(0, dy + dh, w, h - dy - dh, ImageWidget.MATTE_COLOR)  # bottom
        painter.fillRect(0, dy, dx, dh, ImageWidget.MATTE_COLOR)        # left
        painter.fillRect(dx + dw, dy, w - dx - dw, dh, ImageWidget.MATTE_COLOR)  # right
        painter.end()


# ══════════════════════════════════════════════════════════════════════════
#  Main window
# ══════════════════════════════════════════════════════════════════════════

class SlideshowWindow(QMainWindow):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()
        self.args = args
        self.playlist: list[Path] = []
        self.index = 0
        self.current_is_video = False
        self._last_video_fps = 0.0

        self.setWindowTitle("Metixel PySide6+mpv Prototype")
        self.setStyleSheet("background-color: black;")
        if args.fullscreen:
            self.showFullScreen()
        else:
            self.resize(1280, 720)
        if args.hide_cursor:
            QGuiApplication.setOverrideCursor(Qt.CursorShape.BlankCursor)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Image widget + mpv render widget (stacked).
        self.image_widget = ImageWidget(central)
        self.mpv_widget = MpvRenderWidget(central, hwdec=args.hwdec, fast=args.fast,
                                          display_fps=args.display_fps)
        layout.addWidget(self.image_widget)
        layout.addWidget(self.mpv_widget)
        self.image_widget.raise_()

        # UI overlay.
        self.overlay = QLabel(central)
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.overlay.setStyleSheet("background: transparent; color: white;")
        self.overlay.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        self.overlay.setGeometry(0, 0, 400, 200)
        self.overlay.setFont(QFont("Sans", 24))

        self.caption = QLabel(central)
        self.caption.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.caption.setStyleSheet("background: transparent; color: rgba(255,255,255,0.85);")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter)
        self.caption.setGeometry(0, 0, 1280, 60)
        self.caption.setFont(QFont("Sans", 16))

        self.toast = QLabel(central)
        self.toast.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.toast.setStyleSheet(
            "background: rgba(20,20,20,0.92); color: white; border-radius: 8px; padding: 8px 16px;"
        )
        self.toast.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.toast.setGeometry(0, 0, 400, 40)
        self.toast.setFont(QFont("Sans", 14))
        self.toast.hide()
        self._toast_effect = QGraphicsOpacityEffect(self.toast)
        self.toast.setGraphicsEffect(self._toast_effect)

        # Crossfade. Use a QGraphicsOpacityEffect on the image widget, NOT
        # windowOpacity — the Wayland plugin doesn't support window opacity
        # ("This plugin does not support setting window opacity"), which
        # leaves the window black.
        self._image_fade_effect = QGraphicsOpacityEffect(self.image_widget)
        self.image_widget.setGraphicsEffect(self._image_fade_effect)
        self._fade = QPropertyAnimation(self._image_fade_effect, b"opacity", self)
        self._fade.setDuration(int(args.crossfade * 1000))
        self._fade.setEasingCurve(QEasingCurve.Type.InOutQuad)

        # Timers.
        self._advance_timer = QTimer(self)
        self._advance_timer.setSingleShot(True)
        self._advance_timer.timeout.connect(self._on_advance_timeout)

        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start(1000)

        self._toast_timer = QTimer(self)
        self._toast_timer.timeout.connect(self._show_toast)
        self._toast_timer.start(15000)

        # Benchmark.
        self._fps_frames = 0
        self._bench_timer = QTimer(self)
        self._bench_timer.timeout.connect(self._benchmark_tick)
        self._bench_timer.start(5000)
        self._cpu_prev = _read_cpu_times()
        self._results: list[dict] = []

        self._load_playlist(args.media)
        if not self.playlist:
            self.overlay.setText("No media found in:\n" + str(args.media))
            return

        self._show_item(0)

    # -- Playlist ----------------------------------------------------------

    def _load_playlist(self, media_dir: Path) -> None:
        media_dir = Path(media_dir)
        if not media_dir.is_dir():
            print(f"[slideshow] WARNING: media dir not found: {media_dir}")
            return
        for p in sorted(media_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS | VIDEO_EXTS:
                self.playlist.append(p)
        print(f"[slideshow] playlist: {len(self.playlist)} items from {media_dir}")

    # -- Item display ------------------------------------------------------

    def _show_item(self, idx: int) -> None:
        if not self.playlist:
            return
        self.index = idx % len(self.playlist)
        item = self.playlist[self.index]
        is_video = item.suffix.lower() in VIDEO_EXTS
        self.current_is_video = is_video
        self.caption.setText(item.name)

        self._stop_video()
        if is_video:
            self._show_video(item)
        else:
            self._show_image(item)

    def _show_image(self, item: Path) -> None:
        self.mpv_widget.hide()
        self.image_widget.show()
        self.image_widget.raise_()
        self.image_widget.set_image(item)
        self._fade.stop()
        self._image_fade_effect.setOpacity(0.0)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        self._schedule_advance(self.args.image_duration)

    def _show_video(self, item: Path) -> None:
        self.image_widget.hide()
        self.mpv_widget.show()
        self.mpv_widget.raise_()
        try:
            # Ensure the GL render context exists BEFORE playback starts.
            # With vo=libmpv, mpv needs the render context active or it
            # deselects the video track ("Video: no video").
            self.mpv_widget.ensure_gl_init()
            self.mpv_widget.set_callbacks(self._on_video_eof, self._on_video_fps)
            self.mpv_widget.play(str(item))
        except Exception as e:  # noqa: BLE001
            print(f"[slideshow] WARNING: mpv failed for {item.name}: {e}")
            self._schedule_advance(1.0)
            return
        self._schedule_advance(self.args.video_max)

    def _stop_video(self) -> None:
        self.mpv_widget.stop()

    def _on_video_eof(self) -> None:
        print(f"[slideshow] video ended: {self.playlist[self.index].name}")
        self._advance()

    def _on_video_fps(self, fps: float) -> None:
        self._last_video_fps = fps

    # -- Advancing ---------------------------------------------------------

    def _schedule_advance(self, seconds: float) -> None:
        self._advance_timer.stop()
        self._advance_timer.start(int(seconds * 1000))

    def _on_advance_timeout(self) -> None:
        self._advance()

    def _advance(self) -> None:
        self._stop_video()
        self._show_item(self.index + 1)

    # -- UI overlay --------------------------------------------------------

    def _update_clock(self) -> None:
        self.overlay.setText(time.strftime("%H:%M"))

    def _show_toast(self) -> None:
        messages = [
            "New photos synced from Immich",
            "Wi-Fi connected",
            "Battery low — 15%",
            "Weather update available",
        ]
        msg = messages[int(time.time()) % len(messages)]
        self.toast.setText(msg)
        self.toast.adjustSize()
        self.toast.move((self.width() - self.toast.width()) // 2, 24)
        self.toast.show()
        self._toast_effect.setOpacity(0.0)
        anim = QPropertyAnimation(self._toast_effect, b"opacity", self)
        anim.setDuration(400)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.start()
        QTimer.singleShot(3000, lambda: self._hide_toast(anim))

    def _hide_toast(self, anim: QPropertyAnimation) -> None:
        anim.stop()
        anim.setDuration(400)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.finished.connect(self.toast.hide)
        anim.start()

    # -- Benchmark ---------------------------------------------------------

    def _benchmark_tick(self) -> None:
        now = _read_cpu_times()
        cpu = _cpu_percent(self._cpu_prev, now)
        self._cpu_prev = now
        mem = _mem_info()
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        used_pct = 100.0 * (1.0 - avail / total) if total > 0 else 0.0

        fps = self._last_video_fps if self.current_is_video else self._fps_frames / 5.0
        self._fps_frames = 0

        line = (
            f"[bench] fps={fps:.1f} cpu={cpu:.1f}% "
            f"mem={used_pct:.1f}% (avail {avail // 1024}MB / {total // 1024}MB)"
        )
        print(line)

        self._results.append(
            {
                "t": time.time(),
                "fps": round(fps, 1),
                "cpu_percent": round(cpu, 1),
                "mem_used_percent": round(used_pct, 1),
                "mem_available_kb": avail,
                "mem_total_kb": total,
                "media": self.playlist[self.index].name if self.playlist else "",
            }
        )
        self._write_results()

    def _write_results(self) -> None:
        try:
            with open(self.args.out, "w", encoding="utf-8") as f:
                json.dump(self._results, f, indent=2)
        except OSError as e:
            print(f"[bench] WARNING: could not write results: {e}")

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop_video()
        self.mpv_widget.destroy_mpv()
        self._write_results()
        super().closeEvent(event)


# ── Entry point ───────────────────────────────────────────────────────────

def main() -> int:
    faulthandler.enable()
    parser = argparse.ArgumentParser(description="PySide6 + mpv slideshow prototype (cage/Wayland)")
    parser.add_argument("--media", type=Path, default=DEFAULT_MEDIA)
    parser.add_argument("--hwdec", type=str, default="auto",
                        help="mpv hardware decode backend (e.g. auto, v4l2m2m, no)")
    parser.add_argument("--fast", action="store_true", default=False,
                        help="Apply aggressive low-power mpv optimisations "
                             "(bilinear scaling, no dithering, no audio) for weak GPUs")
    parser.add_argument("--display-fps", type=float, default=60.0,
                        help="Display refresh rate for video-sync pacing (default 60)")
    parser.add_argument("--image-duration", type=float, default=5.0)
    parser.add_argument("--video-max", type=float, default=15.0)
    parser.add_argument("--crossfade", type=float, default=1.0)
    parser.add_argument("--fullscreen", action="store_true", default=True)
    parser.add_argument("--no-fullscreen", dest="fullscreen", action="store_false")
    parser.add_argument("--hide-cursor", action="store_true", default=True)
    parser.add_argument("--no-hide-cursor", dest="hide_cursor", action="store_false")
    parser.add_argument("--out", type=Path, default=Path("benchmark.json"))
    parser.add_argument("--duration", type=float, default=0.0,
                        help="Auto-quit after N seconds (0 = run forever)")
    parser.add_argument("--screenshot", type=Path, default=None,
                        help="Capture the window to a PNG after 3s, then quit")
    args = parser.parse_args()

    if os.environ.get("QT_QPA_PLATFORM", "") != "eglfs":
        print("[slideshow] NOTE: run with QT_QPA_PLATFORM=eglfs for the embedded path.")

    app = QApplication(sys.argv)
    # CRITICAL: Qt's QApplication constructor resets LC_NUMERIC to the system
    # locale, which breaks libmpv's mpv_create(). Reset it to C AFTER app
    # creation (setting it before does not survive Qt's init).
    locale.setlocale(locale.LC_NUMERIC, "C")
    window = SlideshowWindow(args)
    window.show()
    if args.screenshot:
        QTimer.singleShot(3000, lambda: _capture_and_quit(window, args.screenshot))
    elif args.duration > 0:
        QTimer.singleShot(int(args.duration * 1000), app.quit)
    return app.exec()


def _capture_and_quit(window: "SlideshowWindow", out: Path) -> None:
    """Capture the window to a PNG and quit."""
    try:
        pix = window.grab()
        pix.save(str(out))
        print(f"[slideshow] screenshot saved to {out}")
    except Exception as e:  # noqa: BLE001
        print(f"[slideshow] screenshot failed: {e}")
    window.close()


if __name__ == "__main__":
    sys.exit(main())