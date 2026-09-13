#!/usr/bin/env bash
# GATE-1 prerequisite check: confirm the Qt + mpv render API is usable on this Pi.
# Run on the Pi. Prints one FACT per line so the output is easy to diff.
set -u

echo "=== python ==="
python3 -VV

echo "=== pyside6 ==="
python3 - <<'PY'
try:
    import PySide6
    print("PySide6:", PySide6.__version__)
except Exception as e:
    print("PySide6: MISSING", e)

for mod in ("PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
            "PySide6.QtOpenGLWidgets"):
    try:
        __import__(mod)
        print(f"{mod}: ok")
    except Exception as e:
        print(f"{mod}: FAIL {e}")
PY

echo "=== python-mpv ==="
python3 - <<'PY'
try:
    import mpv
    print("python-mpv file:", getattr(mpv, "__file__", "?"))
    # The render API is what the Qt backend needs. `wid` embedding is X11-only,
    # so a missing MpvRenderContext would mean the whole approach is unavailable.
    print("MpvRenderContext:", hasattr(mpv, "MpvRenderContext"))
    print("MpvGlGetProcAddressFn:", hasattr(mpv, "MpvGlGetProcAddressFn"))
    print("MPV class:", hasattr(mpv, "MPV"))
except Exception as e:
    print("python-mpv: FAIL", e)
PY

echo "=== qt platform plugins ==="
find /usr/lib -path '*qt6/plugins/platforms*' -name '*.so*' 2>/dev/null | sort

echo "=== libmpv ==="
ldconfig -p 2>/dev/null | grep -i libmpv || echo "(ldconfig unavailable)"

echo "=== mpv binary + hwdec list ==="
command -v mpv && mpv --version | head -2
echo "-- hwdec options reported by mpv --"
mpv --hwdec=help 2>&1 | head -20

echo "=== vaapi/v4l2 device nodes ==="
ls -1 /dev/video* 2>/dev/null || echo "(no /dev/video*)"
ls -1 /dev/dri/ 2>/dev/null

echo "=== x264/x265 encoders (for the transcode profile decision) ==="
ffmpeg -hide_banner -encoders 2>/dev/null | grep -E 'libx264|libx265|h264_v4l2m2m|hevc_v4l2m2m' || echo "(ffmpeg missing)"
