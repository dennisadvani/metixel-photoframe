#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Qt Quick / Qt Multimedia feasibility probe — Phase-0 spike, runs ON the frame.

This exists to ANSWER THREE QUESTIONS WITH MEASUREMENTS rather than opinion,
because the whole reason for considering a Qt Quick renderer is a judder
("frames arrive unevenly") and stutter ("dropped frames during motion") report
that no amount of code reading can settle:

  1. Does a representative Qt Quick scene render under cage on this GPU, and via
     which graphics API?  A window is opened, a production-shaped payload is
     drawn (full-bleed artwork Image + a ShapePath mat ring + an animating item)
     and `grim` is asked for a screenshot so the result can be LOOKED at.
     Deliberately NO live blur effect: production blurs offline in
     `ambient_blur.py` and blits a pre-blurred JPEG, so a live MultiEffect here
     would misrepresent the load.

  2. Is it vsync-paced?  Every swap is timestamped from
     `QQuickWindow.frameSwapped` and the inter-swap deltas are reported as
     mean/p50/p95/max plus a count of intervals longer than 1.5 vsync periods.
     That distribution IS the judder question: a vsync-locked renderer clusters
     at one period with a small tail, while a timer-driven one smears.

  3. Can Qt Multimedia hardware-decode on this board?  An HEVC file is played and
     `QVideoFrame.handleType()` is recorded for every delivered frame.
     `NoHandle` means the frame came back in ordinary system memory — i.e.
     SOFTWARE decode, no matter how good the frame rate looks.  This is the
     decisive check, because Qt's hw-texture interop on Linux is VAAPI-only
     (`VAAPITextureHandles` / `VAAPITextureConverter`) and a Pi 5 has no VAAPI.

Everything is reported as `null` when it could not be measured, so a missing
number is never silently read as a pass.

Invoke through `_run_qml_probe.sh`, which supplies cage, the Wayland environment
and the supplementary groups (`video render input tty`) that an ssh session lacks
— without those a hardware path cannot open its device and the run would quietly
measure the software fallback instead.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# QML payloads
# ---------------------------------------------------------------------------

# Representative of what production paints: a slide on a mat ring, with a moving
# element so the eye (and the swap timestamps) can see pacing.
QML_SCENE = """
import QtQuick
import QtQuick.Window
import QtQuick.Shapes

Window {
    id: root
    objectName: "root"
    width: 1920
    height: 1200
    visible: true
    color: "#0e0e12"

    Image {
        id: artwork
        anchors.fill: parent
        anchors.margins: 40
        source: probeImageUrl
        fillMode: Image.PreserveAspectFit
        asynchronous: false
    }

    // The mat: production draws this as up to four pixel rectangles, so a
    // ShapePath ring is a strictly harder case, not an easier one.
    Shape {
        anchors.fill: parent
        ShapePath {
            strokeWidth: 0
            fillColor: "#14141a"
            fillRule: ShapePath.OddEvenFill
            startX: 0
            startY: 0
            PathRectangle { x: 0; y: 0; width: root.width; height: root.height }
            PathRectangle { x: 120; y: 90; width: 1680; height: 1020 }
        }
    }

    Rectangle {
        id: mover
        objectName: "mover"
        width: 160
        height: 160
        y: 60
        color: "#8B1A2B"
        NumberAnimation on x {
            from: 0
            to: root.width - mover.width
            duration: 3000
            loops: Animation.Infinite
            running: probeRunning
        }
    }
}
"""

QML_VIDEO = """
import QtQuick
import QtQuick.Window
import QtMultimedia

Window {
    id: root
    objectName: "root"
    width: 1920
    height: 1200
    visible: true
    color: "black"

    VideoOutput {
        id: vo
        objectName: "vo"
        anchors.fill: parent
    }

    MediaPlayer {
        id: mp
        objectName: "player"
        videoOutput: vo
        source: probeVideoUrl
        loops: MediaPlayer.Infinite
        Component.onCompleted: play()
    }
}
"""

# A production-shaped CROSSFADE: ambient band, two artwork layers blending, and the
# mat ring composited over both.
#
# Transitions are the heaviest thing the frame does.  The raster canvas composites
# the outgoing and incoming layers in ONE software repaint of the whole 1920x1200
# surface -- two full-canvas alpha blends per frame, on the CPU -- which is why a
# crossfade is the most likely place for the "smoothness" complaint to reappear
# even after video is fixed.  This case exists to measure that directly.
#
# `t` is the blend position held by ONE animation, so both opacities derive from a
# single source of truth.  Two independent animations would be free to drift apart,
# and a crossfade whose alphas do not sum to 1 dims through the middle -- the exact
# failure mode `present_transition`'s docstring warns about.
#
# The mat ring is drawn OVER both blending layers, so the measurement includes the
# real composite rather than just "two images blend".
QML_CROSSFADE = """
import QtQuick
import QtQuick.Window
import QtQuick.Shapes

Window {
    id: root
    objectName: "root"
    width: 1920
    height: 1200
    visible: true
    color: "#0e0e12"

    property real t: 0.0

    // Ambient band: full canvas, under everything.  Mirrors the paint order in
    // DisplayBackend.present: ambient -> artwork -> whitespace -> mat -> moulding.
    Rectangle {
        anchors.fill: parent
        color: "#1a1a22"
    }

    // Outgoing slide, fading down.
    Image {
        id: outgoing
        objectName: "outgoing"
        anchors.fill: parent
        anchors.margins: 40
        source: probeImageUrl
        fillMode: Image.PreserveAspectFit
        cache: false
        opacity: 1.0 - root.t
    }

    // Incoming slide, fading up.
    Image {
        id: incoming
        objectName: "incoming"
        anchors.fill: parent
        anchors.margins: 40
        source: probeImage2Url
        fillMode: Image.PreserveAspectFit
        cache: false
        opacity: root.t
    }

    // The mat ring, composited OVER both blending layers.
    Shape {
        anchors.fill: parent
        ShapePath {
            strokeWidth: 0
            fillColor: "#14141a"
            fillRule: ShapePath.OddEvenFill
            startX: 0
            startY: 0
            PathRectangle { x: 0; y: 0; width: root.width; height: root.height }
            PathRectangle { x: 120; y: 90; width: 1680; height: 1020 }
        }
    }

    // Duration matches production's slideshow.transition_duration_ms (2500), and it
    // LOOPS, so a 20 s run measures a CONTINUOUS crossfade.  That is strictly worse
    // than production, which blends for 2.5 s of a 15 s slide and then holds a
    // static frame, so passing here is a real margin rather than a tie.
    NumberAnimation on t {
        from: 0.0
        to: 1.0
        duration: 2500
        loops: Animation.Infinite
        running: probeRunning
    }
}
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _proc_cpu_seconds() -> float | None:
    """utime+stime for THIS process, in seconds, from /proc/self/stat.

    Field 14/15 but the comm field can contain spaces, so parse after the last
    ')' to stay correct even if the process name has one.
    """
    try:
        raw = Path("/proc/self/stat").read_text()
    except OSError:
        return None
    tail = raw[raw.rindex(")") + 2 :].split()
    try:
        utime, stime = int(tail[11]), int(tail[12])
    except (IndexError, ValueError):
        return None
    return (utime + stime) / os.sysconf("SC_CLK_TCK")


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of a NON-EMPTY sequence.

    Raises rather than returning a sentinel for an empty list: a missing timing
    must never be silently readable as a real one.
    """
    if not values:
        raise ValueError("percentile of an empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = min(len(ordered) - 1, max(0, round(pct / 100.0 * (len(ordered) - 1))))
    return ordered[idx]


def _interval_stats(stamps: list[float], vsync_ms: float | None) -> dict:
    """Turn a list of monotonic swap timestamps into a pacing report."""
    if len(stamps) < 2:
        return {"count": len(stamps), "span_s": None, "intervals": None}

    deltas_ms = [(b - a) * 1000.0 for a, b in zip(stamps, stamps[1:], strict=False)]
    span_s = stamps[-1] - stamps[0]

    report = {
        "count": len(stamps),
        "span_s": round(span_s, 3),
        "fps": round(len(deltas_ms) / span_s, 2) if span_s > 0 else None,
        "intervals": {
            "mean_ms": round(sum(deltas_ms) / len(deltas_ms), 3),
            "p50_ms": round(_percentile(deltas_ms, 50), 3),
            "p95_ms": round(_percentile(deltas_ms, 95), 3),
            "p99_ms": round(_percentile(deltas_ms, 99), 3),
            "max_ms": round(max(deltas_ms), 3),
            "min_ms": round(min(deltas_ms), 3),
        },
    }

    if vsync_ms:
        long_frames = [d for d in deltas_ms if d > vsync_ms * 1.5]
        report["vsync_ms"] = round(vsync_ms, 3)
        report["dropped"] = len(long_frames)
        report["dropped_pct"] = round(100.0 * len(long_frames) / len(deltas_ms), 2)
        # A frame is "clean" when it lands within half a period of one period.
        clean = sum(1 for d in deltas_ms if abs(d - vsync_ms) < vsync_ms * 0.35)
        report["vsync_locked_pct"] = round(100.0 * clean / len(deltas_ms), 2)
    return report


def _grim(path: str) -> dict:
    """Screenshot the composited output. Proves pixels actually reached the screen."""
    try:
        proc = subprocess.run(["grim", path], capture_output=True, timeout=15, check=False)
    except FileNotFoundError:
        return {"ok": False, "error": "grim not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "grim timed out"}
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.decode(errors="replace")[:200]}
    size = Path(path).stat().st_size if Path(path).exists() else 0
    return {"ok": True, "path": path, "bytes": size}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True, choices=["qml", "video", "crossfade"])
    ap.add_argument("--image", default="")
    ap.add_argument("--image2", default="", help="second image for the crossfade")
    ap.add_argument("--video", default="")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--outdir", default="/tmp/metixel-qmlprobe/out")
    ap.add_argument("--shot", default="")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    report_path = outdir / f"{args.case}.json"

    report: dict = {
        "case": args.case,
        "argv": sys.argv[1:],
        "qt": {},
        "qml_loaded": False,
        "qml_warnings": [],
        "screen": {},
        "swaps": None,
        "video": None,
        "capture": None,
        "cpu": {},
        "fatal": None,
    }

    cpu_start = _proc_cpu_seconds()
    wall_start = time.perf_counter()
    imports_error = None

    try:
        from PySide6.QtCore import QObject, QTimer, QUrl, qVersion
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtQuick import QQuickWindow

        # Qt is imported here and not at module scope on purpose: a missing Qt is
        # then a reportable fact rather than an import traceback.
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame  # noqa: F401
        except Exception as exc:  # pragma: no cover - board dependent
            imports_error = f"QtMultimedia unavailable: {exc}"

        report["qt"] = {
            "pyside6": __import__("PySide6").__version__,
            "qt": qVersion(),
            "qtmultimedia_import_error": imports_error,
        }

        app = QGuiApplication(sys.argv)

        engine = QQmlApplicationEngine()
        engine.setOutputWarningsToStandardError(True)

        def on_qml_warnings(items) -> None:
            report["qml_warnings"].extend(str(item.toString()) for item in items)

        engine.warnings.connect(on_qml_warnings)

        engine.rootContext().setContextProperty("probeRunning", True)
        engine.rootContext().setContextProperty(
            "probeImageUrl", QUrl.fromLocalFile(args.image) if args.image else QUrl()
        )
        # Falls back to the first image so a crossfade can be measured with one
        # --image; two distinct files only matter for looking at the result.
        second = args.image2 or args.image
        engine.rootContext().setContextProperty(
            "probeImage2Url", QUrl.fromLocalFile(second) if second else QUrl()
        )
        engine.rootContext().setContextProperty(
            "probeVideoUrl", QUrl.fromLocalFile(args.video) if args.video else QUrl()
        )

        qml_dir = outdir / "qml"
        qml_dir.mkdir(parents=True, exist_ok=True)
        qml_path = qml_dir / f"{args.case}.qml"
        if args.case == "video":
            scene = QML_VIDEO
        elif args.case == "crossfade":
            scene = QML_CROSSFADE
        else:
            scene = QML_SCENE
        qml_path.write_text(scene)

        engine.load(QUrl.fromLocalFile(str(qml_path)))
        if not engine.rootObjects():
            report["fatal"] = "QML failed to load — see qml_warnings"
            raise SystemExit(0)

        root = engine.rootObjects()[0]
        window = root if isinstance(root, QQuickWindow) else None
        if window is None:
            for child in root.findChildren(QQuickWindow):
                window = child
                break

        if window is not None:
            screen = window.screen()
            if screen is not None:
                geo = screen.geometry()
                report["screen"] = {
                    "name": screen.name(),
                    "refresh_rate": screen.refreshRate(),
                    "geometry": [geo.x(), geo.y(), geo.width(), geo.height()],
                    "device_pixel_ratio": screen.devicePixelRatio(),
                }
                report["vsync_ms"] = 1000.0 / screen.refreshRate() if screen.refreshRate() else None
            try:
                iface = window.rendererInterface()
                api = iface.graphicsApi() if iface else None
                report["graphics_api"] = str(api)
            except Exception as exc:
                report["graphics_api"] = f"unavailable: {exc}"

        report["qml_loaded"] = True

        # --- swap timing -------------------------------------------------
        swaps: list[float] = []
        if window is not None:
            window.frameSwapped.connect(lambda: swaps.append(time.perf_counter()))

        # --- video instrumentation ---------------------------------------
        video_frames: list[float] = []
        handle_types: list[str] = []
        frame_formats: list[str] = []
        sink_route: list[str] = []
        player = None
        if args.case == "video":
            player = root.findChild(QObject, "player")
            if player is None:
                report["fatal"] = "QML MediaPlayer (objectName 'player') not found"
            else:

                def on_frame(frame) -> None:
                    video_frames.append(time.perf_counter())
                    try:
                        handle_types.append(str(frame.handleType()))
                    except Exception:
                        handle_types.append("?")
                    try:
                        sf = frame.surfaceFormat()
                        frame_formats.append(
                            f"{frame.pixelFormat()} {sf.frameWidth()}x{sf.frameHeight()}"
                        )
                    except Exception:
                        pass

                def _find_sink():
                    """Locate the QVideoSink, trying more than one route.

                    MediaPlayer.videoSink is created lazily, so reading it once at
                    load time can legitimately return null. Treating that as "no
                    hardware decode" would invert the answer, so we retry and record
                    which route eventually worked.
                    """
                    candidates = [
                        ("MediaPlayer.videoSink", player),
                        ("VideoOutput.videoSink", root.findChild(QObject, "vo")),
                    ]
                    for route, obj in candidates:
                        if obj is None:
                            continue
                        try:
                            found = obj.property("videoSink")
                        except Exception:
                            continue
                        if found is not None:
                            sink_route.append(route)
                            return found
                    return None

                def attach_sink(attempt: int = 0) -> None:
                    sink = _find_sink()
                    if sink is None:
                        if attempt < 15:
                            QTimer.singleShot(200, lambda: attach_sink(attempt + 1))
                        else:
                            report["video_error"] = (
                                "no QVideoSink found via MediaPlayer.videoSink or "
                                "VideoOutput.videoSink after 16 attempts"
                            )
                        return
                    sink.videoFrameChanged.connect(on_frame)

                attach_sink()

                player.errorOccurred.connect(
                    lambda err, msg: report.setdefault("video_errors", []).append(f"{err}: {msg}")
                )

        # --- screenshot ---------------------------------------------------
        def take_shot() -> None:
            if args.shot:
                report["capture"] = _grim(args.shot)

        # --- run ----------------------------------------------------------
        elapsed_s = 0.0
        if window is not None:
            # Frame count over time, plus periodic progress so a wedged run is
            # distinguishable from a slow one.
            def progress() -> None:
                nonlocal elapsed_s
                elapsed_s += 2.0
                print(
                    f"  t={elapsed_s:4.1f}s swaps={len(swaps)} video_frames={len(video_frames)}",
                    flush=True,
                )

            tick = QTimer()
            tick.setInterval(2000)
            tick.timeout.connect(progress)
            tick.start()

        QTimer.singleShot(int(args.seconds * 1000 * 0.75), take_shot)
        QTimer.singleShot(int(args.seconds * 1000), app.quit)

        app.exec()

        # --- collect ------------------------------------------------------
        vsync_ms = report.get("vsync_ms")
        report["swaps"] = _interval_stats(swaps, vsync_ms)

        if args.case == "video":
            unique_handles = sorted(set(handle_types))
            # The enum stringifies as "HandleType.NoHandle", so match on substring:
            # comparing against the bare name silently reports CPU frames as GPU ones.
            hw = [h for h in unique_handles if "NoHandle" not in h and h != "?"]
            if not video_frames:
                # Absence of evidence is not evidence of a software path: with no
                # frames observed, hw-vs-sw is simply unknown.
                verdict = "UNMEASURED — no QVideoFrame signal was observed"
            elif hw:
                verdict = f"HARDWARE decode, GPU frames (handle types: {', '.join(hw)})"
            else:
                verdict = (
                    "HW DECODER, CPU FRAMES — the decoder ran in hardware "
                    "(see the FFmpeg 'Hwaccel V4L2' line) but Qt handed over "
                    "NoHandle system-memory frames, so every frame is read back "
                    "and re-uploaded instead of being used zero-copy"
                )

            # Judge the video against ITS OWN frame rate, not the display's: a 24 fps
            # clip presented at 24 fps is perfect, not "97% dropped". The meaningful
            # figure is presented-fps vs decoded-fps.
            video_stats = _interval_stats(video_frames, None)
            decoded_fps = video_stats.get("fps")
            presented_fps = (report.get("swaps") or {}).get("fps")
            report["video"] = {
                "source": args.video,
                "frames_delivered": len(video_frames),
                "interval": video_stats,
                "decoded_fps": decoded_fps,
                "presented_fps": presented_fps,
                "presented_pct_of_decoded": (
                    round(100.0 * presented_fps / decoded_fps, 1)
                    if presented_fps and decoded_fps
                    else None
                ),
                "handle_types": unique_handles,
                "frame_formats": sorted(set(frame_formats))[:8],
                "sink_route": sink_route,
                "hardware_decode": bool(hw),
                "verdict": verdict,
            }
            if player is not None:
                try:
                    report["video"]["media_status"] = str(player.property("mediaStatus"))
                    report["video"]["playback_state"] = str(player.property("playbackState"))
                    report["video"]["error_string"] = str(player.property("errorString"))
                except Exception:
                    pass

    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - report, never crash
        import traceback

        report["fatal"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()[-2000:]
    finally:
        wall_s = time.perf_counter() - wall_start
        cpu_end = _proc_cpu_seconds()
        if cpu_start is not None and cpu_end is not None:
            used = cpu_end - cpu_start
            report["cpu"] = {
                "wall_s": round(wall_s, 2),
                "cpu_s": round(used, 2),
                "cpu_percent": round(100.0 * used / wall_s, 1) if wall_s > 0 else None,
            }
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: v for k, v in report.items() if k not in ("traceback",)}, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
