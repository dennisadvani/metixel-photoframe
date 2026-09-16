"""Mutation-test the blur-backdrop guards.

Every mutation must make the blur tests fail.  A mutation that survives means the
corresponding test is not actually guarding anything.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

CANVAS = pathlib.Path("src/metixel/display/qt_canvas.py")
PRESENTER = pathlib.Path("src/metixel/frontend/presentation/presenter.py")
TESTS = "testing/unit_tests/display/test_blur_backdrop.py"

MUTATIONS: list[tuple[str, pathlib.Path, str, str]] = [
    (
        "no-cache: rebuild the blur every frame",
        CANVAS,
        "        if self._blur_key == key and self._blur_pixmap is not None:\n            return self._blur_pixmap\n        if self._prev_blur_key == key and self._prev_blur_pixmap is not None:\n            return self._prev_blur_pixmap",
        "        if False:\n            return self._blur_pixmap",
    ),
    (
        "darken-in-key: brightness invalidates the blur",
        CANVAS,
        "        key = (id(image), target_w, target_h, round(radius, 2))",
        "        key = (id(image), target_w, target_h, round(radius, 2), plan.ambient_darken)",
    ),
    (
        "keep-aspect: letterbox gaps return",
        CANVAS,
        "        stretched = image.scaled(\n            target_w,\n            target_h,\n            Qt.AspectRatioMode.IgnoreAspectRatio,\n            Qt.TransformationMode.SmoothTransformation,\n        )",
        "        stretched = image.scaled(\n            target_w,\n            target_h,\n            Qt.AspectRatioMode.KeepAspectRatio,\n            Qt.TransformationMode.SmoothTransformation,\n        )",
    ),
    (
        "downscale-blur: the blocking artefacts return",
        CANVAS,
        "        blurred = _blur_qimage(stretched, radius)",
        "        blurred = stretched.scaled(\n"
        "            max(1, int(target_w / radius)),\n"
        "            max(1, int(target_h / radius)),\n"
        "            Qt.AspectRatioMode.IgnoreAspectRatio,\n"
        "            Qt.TransformationMode.SmoothTransformation,\n"
        "        )",
    ),
    (
        "expensive-blur: Gaussian instead of the measured-cheaper Box",
        CANVAS,
        "            blurred = source.filter(ImageFilter.BoxBlur(radius))",
        "            blurred = source.filter(ImageFilter.GaussianBlur(radius))",
    ),
    (
        "inverted-radius: larger radius becomes LESS blur",
        CANVAS,
        "            blurred = source.filter(ImageFilter.BoxBlur(radius))",
        "            blurred = source.filter(ImageFilter.BoxBlur(max(1.0, 100.0 - radius)))",
    ),
    (
        "leak: never release the backdrop",
        CANVAS,
        "        if image is not self._blur_image:",
        "        if False:",
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
        "z-order: incoming backdrop painted above the incoming artwork",
        CANVAS,
        "                    if plan.ambient_strategy == \"blur\":\n"
        "                        self._draw_backdrop_layer(painter, plan, self._image, self._image_alpha)\n"
        "                    painter.setOpacity(self._image_alpha)",
        "                    painter.setOpacity(self._image_alpha)",
    ),
    (
        "z-order: incoming backdrop painted before the outgoing artwork",
        CANVAS,
        "                if self._prev_plan is not None and self._prev_alpha > 0.01:\n"
        "                    if self._prev_plan.ambient_strategy == \"blur\":",
        "                if self._image is not None and self._image_alpha > 0.01:\n"
        "                    if plan.ambient_strategy == \"blur\":\n"
        "                        self._draw_backdrop_layer(painter, plan, self._image, self._image_alpha)\n"
        "                if self._prev_plan is not None and self._prev_alpha > 0.01:\n"
        "                    if self._prev_plan.ambient_strategy == \"blur\":",
    ),
    (
        "outgoing-backdrop: use the incoming image for the outgoing layer",
        CANVAS,
        "self._draw_backdrop_layer(\n"
        "                            painter, self._prev_plan, self._prev_image, self._prev_alpha\n"
        "                        )",
        "self._draw_backdrop_layer(\n"
        "                            painter, self._prev_plan, self._image, self._prev_alpha\n"
        "                        )",
    ),
    (
        "no-band-clip: the backdrop covers its own photo",
        CANVAS,
        "            painter.setClipRegion(region)\n",
        "",
    ),
]


def run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TESTS, "-q", "--no-cov", "-p", "no:cacheprovider"],
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
