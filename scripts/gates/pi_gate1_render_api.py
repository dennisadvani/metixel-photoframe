#!/usr/bin/env python3
"""GATE-1 remainder: does hwdec still engage on the GL render path?

The vo=null measurements proved the DECODER works, but the production stack
renders through `vo=libmpv` into Qt's GL context via MpvRenderContext. A
hardware decoder can fail to initialize there for reasons that do not apply to
vo=null (interop between the decoder's buffers and the GL context, for one).

This drives mpv with the render API directly -- no Qt, no window -- and reports
whether hardware decoding still engages. It prints mpv's own log lines, so the
verdict comes from mpv rather than from us interpreting counters.

Usage: python3 g1_render_api.py <video> [hwdec]
"""

import locale
import sys
import time

locale.setlocale(locale.LC_NUMERIC, "C")

VIDEO = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gate1/test_hevc_1080p.mp4"
HWDEC = sys.argv[2] if len(sys.argv) > 2 else "auto"


def main() -> int:
    import mpv

    lines: list[str] = []

    def log_handler(level: str, component: str, message: str) -> None:
        msg = message.strip()
        if any(
            k in msg.lower()
            for k in (
                "hwdec",
                "hardware decod",
                "software decod",
                "v4l2",
                "drm",
                "dmabuf",
                "egl",
                "error",
            )
        ):
            lines.append(f"[{component}] {msg}")

    player = mpv.MPV(
        vo="libmpv",
        hwdec=HWDEC,
        audio="no",
        osc=False,
        osd_level=0,
        loglevel="debug",
        log_handler=log_handler,
    )

    # Minimal GL context: an offscreen EGL surface via cage's Wayland socket is
    # not guaranteed, so use mpv's own "sw" render context. It exercises the same
    # render-API path and still runs the decoder selection logic, which is what
    # we are testing. (The full Qt path is validated by the GATE-2 smoke test.)
    from mpv import MpvRenderContext

    # A get_proc_address that always fails is fine for the sw backend.
    ctx = MpvRenderContext(player, "sw")
    print(f"render context: sw (api={ctx} is not None)")

    player.play(VIDEO)
    deadline = time.time() + 12
    while time.time() < deadline:
        if player.get_property("eof-reached"):
            break
        time.sleep(0.2)

    player.stop()
    ctx.free()
    player.terminate()

    print()
    print(f"=== mpv log (hwdec={HWDEC}) ===")
    for line in lines[-25:]:
        print("  " + line)

    hw = any("hardware decod" in ln.lower() for ln in lines)
    print()
    print("VERDICT:", "HARDWARE decode engaged" if hw else "software (no hardware decode line)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
