"""Mutation-test the blur-backdrop guards.

Every mutation must make the blur tests fail.  A mutation that survives means the
corresponding test is not actually guarding anything.
"""

# The mutation patterns below are verbatim slices of the guarded source, so they
# are exactly as long as the code they quote.  Wrapping them would defeat the
# point: a pattern that no longer matches the source silently becomes a SKIP.
# ruff: noqa: E501

from __future__ import annotations

import pathlib
import subprocess
import sys

CANVAS = pathlib.Path("src/metixel/display/qt_canvas.py")
AMBIENT = pathlib.Path("src/metixel/display/ambient_blur.py")
PRESENTER = pathlib.Path("src/metixel/frontend/presentation/presenter.py")
RENDERER = pathlib.Path("src/metixel/frontend/renderer.py")
TESTS = [
    "testing/unit_tests/display/test_blur_backdrop.py",
    "testing/unit_tests/display/test_backdrop_warm.py",
    # The artwork guards share ``qt_canvas.py`` with the backdrop ones, and the
    # crop they pin is on the same draw path — a canvas mutation can break either.
    "testing/unit_tests/display/test_prescaled_artwork.py",
]

MUTATIONS: list[tuple[str, pathlib.Path, str, str]] = [
    (
        "unthrottled: the cpu cap is dropped",
        AMBIENT,
        "WORKER_CPU_LIMIT = 50",
        "WORKER_CPU_LIMIT = None",
    ),
    (
        "identity-blind: the blur radius is dropped from the identity",
        AMBIENT,
        '            f"|{self.width}x{self.height}|{self.radius}|{self.blur_filter}"',
        '            f"|{self.width}x{self.height}|{self.blur_filter}"',
    ),
    (
        "identity-blind: the source fingerprint is dropped",
        AMBIENT,
        '            f"{self.source}|{self.mtime_ns}|{self.size}"',
        '            f"{self.source}"',
    ),
    (
        "identity-blind: the blur kernel is dropped from the identity",
        AMBIENT,
        '            f"|{self.width}x{self.height}|{self.radius}|{self.blur_filter}"',
        '            f"|{self.width}x{self.height}|{self.radius}"',
    ),
    (
        "timeout-blind: a wedged child holds the slide forever",
        AMBIENT,
        "            if time.monotonic() - self._started_at <= self._timeout_s:",
        "            if True:",
    ),
    (
        "keep-aspect: letterbox gaps return",
        AMBIENT,
        "        stretched = artwork.resize((max(1, int(width)), max(1, int(height))))",
        "        stretched = artwork.copy()",
    ),
    (
        "downscale-blur: the blocking artefacts return",
        AMBIENT,
        "            blurred = stretched.filter(ImageFilter.BoxBlur(pixels))",
        "            blurred = stretched.resize("
        "(max(1, int(stretched.width / 40)), max(1, int(stretched.height / 40))))",
    ),
    (
        "filter-ignored: the user's blur kernel is dropped",
        AMBIENT,
        '        if resolve_filter(blur_filter) == "gaussian":',
        "        if False:",
    ),
    (
        "inverted-radius: larger radius becomes LESS blur",
        AMBIENT,
        "            blurred = stretched.filter(ImageFilter.BoxBlur(pixels))",
        "            blurred = stretched.filter(ImageFilter.BoxBlur(100.0 - pixels))",
    ),
    (
        "unclamped-radius: a hand-edited config reaches the filter",
        AMBIENT,
        "    return max(MIN_RADIUS, min(MAX_RADIUS, float(radius)))",
        "    return float(radius)",
    ),
    (
        "blanks: a missing backdrop no longer falls back to the flat fill",
        CANVAS,
        "        if pixmap is None or pixmap.isNull():",
        "        if False:",
    ),
    (
        "holds-forever: a failed backdrop is waited on anyway",
        CANVAS,
        "        if request is None or request.job_id in self._backdrop_failed:",
        "        if request is None:",
    ),
    (
        "supersede-leak: the superseded child keeps running",
        AMBIENT,
        "        if self._job_id == request.job_id and self._running():\n            return\n        self.cancel()",
        "        if self._job_id == request.job_id and self._running():\n            return",
    ),
    (
        "release-leak: finished backdrops are never deleted from tmpfs",
        AMBIENT,
        "        self.output_path(job_id).unlink(missing_ok=True)",
        "        pass",
    ),
    (
        "no-transition-guard: blur work runs during a crossfade",
        PRESENTER,
        "        if collect is None or warm is None or self._in_transition():",
        "        if collect is None or warm is None:",
    ),
    (
        "boot-gate-open: a second blur starts while the boot screen waits",
        PRESENTER,
        "        if not self._boot_complete:",
        "        if False:",
    ),
    (
        "hold-removed: the transition no longer waits for the backdrop",
        PRESENTER,
        '        ready = getattr(self._backend, "backdrop_ready", None)',
        "        ready = None",
    ),
    (
        "boot-ignores-backdrop: the fade no longer waits for the first blur",
        RENDERER,
        "                and self._presentation.ambient_ready\n",
        "",
    ),
    (
        "band-over-blur: flat ambient fill painted over the blur band",
        CANVAS,
        'if plan.ambient is not None and plan.ambient_strategy != "blur":',
        "if plan.ambient is not None:",
    ),
    (
        "curtain-in-blur: flat curtain wipes the blurred band",
        CANVAS,
        '                        and plan.ambient_strategy != "blur"\n',
        "",
    ),
    (
        "curtain-removed: no residue wipe in solid/bars",
        CANVAS,
        "                        self._paint_transition_curtain(painter, plan, self._image_alpha)",
        "                        pass",
    ),
    (
        "hardcoded-spec: user blur amount is dropped",
        pathlib.Path("src/metixel/framing/framing_templates.py"),
        "            blur_radius=float(ambient_blur_radius),\n            darken=float(ambient_darken),",
        "            darken=0.35,",
    ),
    (
        "reload-blind: a blur change never rebuilds the engine",
        PRESENTER,
        "            self._layout.ambient_blur_radius,\n            self._layout.ambient_darken,",
        "",
    ),
    (
        "z-order: the incoming backdrop is never painted",
        CANVAS,
        '                    if plan.ambient_strategy == "blur":\n'
        "                        self._draw_backdrop_layer(\n"
        "                            painter,\n"
        "                            plan,\n"
        "                            self._backdrop_for(self._image),\n"
        "                            self._image_alpha,\n"
        "                        )",
        "                    pass",
    ),
    (
        "outgoing-backdrop: the incoming artwork's blur is used for the outgoing layer",
        CANVAS,
        "                            self._backdrop_for(self._prev_image),",
        "                            self._backdrop_for(self._image),",
    ),
    (
        "no-band-clip: the backdrop covers its own photo",
        CANVAS,
        "        region = QRegion(self.rect()).subtracted(QRegion(_int_rect(plan.artwork_dst)))\n        if region.isEmpty():\n            return\n\n        painter.save()\n        try:\n            painter.setClipRegion(region)\n            painter.setOpacity",
        "        region = QRegion(self.rect())\n        if region.isEmpty():\n            return\n\n        painter.save()\n        try:\n            painter.setClipRegion(region)\n            painter.setOpacity",
    ),
    (
        # A video is laid out against the VIDEO's dimensions but drawn as the
        # poster ffmpeg already shrank to fit the screen, so a window in media
        # pixels runs past the image — and ``QImage.copy`` pads the overhang black
        # rather than clipping it.
        "crop-in-media-space: the crop ignores the image's pixel space",
        CANVAS,
        "        window = _int_rect(plan.source_window(image.width(), image.height()))\n        cropped = image.copy(window)",
        "        window = _int_rect(plan.artwork_src)\n        cropped = image.copy(window)",
    ),
]


def run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-q", "--no-cov", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    originals = {path: path.read_text(encoding="utf-8") for path in {m[1] for m in MUTATIONS}}

    code, output = run_tests()
    if code != 0:
        print("BASELINE FAILED — fix the tests first")
        print(output[-3000:])
        return 1
    print("baseline: PASS\n")

    missed = 0
    for label, path, old, new in MUTATIONS:
        original = originals[path]
        if old not in original:
            print(f"SKIP    {label}: pattern not found")
            missed += 1
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        code, _ = run_tests()
        caught = code != 0
        print(f"{'CAUGHT' if caught else 'MISSED'}  {label}")
        if not caught:
            missed += 1
        path.write_text(original, encoding="utf-8")

    for path, text in originals.items():
        path.write_text(text, encoding="utf-8")

    print(f"\n{'all mutations caught' if missed == 0 else f'{missed} NOT caught'}")
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
