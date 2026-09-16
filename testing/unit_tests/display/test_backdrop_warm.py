# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Guards for backdrop pre-warming and the transition hold.

The defect these exist for: transitions were choppy because the blurred backdrop
was built lazily inside ``paintEvent``, on the GUI thread, on the FIRST frame of
every crossfade.  Measured at ~56 ms for a 1920x1200 backdrop, that is several
dropped frames exactly when the animation is most visible — so the whole
crossfade juddered.

Two halves to the fix, and both need guarding because either alone is useless:

1. **Warm ahead of time.**  The next item's backdrop is built on a worker thread
   while the current slide is showing, so the cost is paid off the render thread.
2. **Do not start the transition until it is ready.**  The presenter holds the
   slide rather than beginning a crossfade against a backdrop that does not
   exist yet.  Without this, warming is merely an optimisation and a slow device
   still paints a flat band and then repaints mid-transition — a flicker.

The blocking hazard matters as much as the feature: the readiness check runs
every frame while a slide is held, and the presenter's image lookup has a
BLOCKING fallback, so using the wrong accessor there would reintroduce the stall
in a new place.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_CANVAS = _ROOT / "src" / "metixel" / "display" / "qt_canvas.py"
_BACKEND = _ROOT / "src" / "metixel" / "display" / "qt_backend.py"
_PRESENTER = _ROOT / "src" / "metixel" / "frontend" / "presentation" / "presenter.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function(path: Path, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(_source(path))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path.name}")


def _code(path: Path, name: str) -> str:
    """The function's executable statements with docstring and comments stripped."""
    node = _function(path, name)
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


class TestTheWarmWorkerRunsOffTheGuiThread:
    def test_the_blur_happens_on_a_thread(self) -> None:
        code = _code(_CANVAS, "warm_backdrop")
        assert "threading.Thread" in code, "the blur must not run on the caller's thread"
        assert ".start()" in code
        assert "daemon=True" in code, "a stuck warm must not keep the process alive"

    def test_the_worker_builds_a_payload_not_a_pixmap(self) -> None:
        """``QPixmap`` is a GUI-thread resource; only ``QImage`` may cross over."""
        code = _code(_CANVAS, "_warm_worker")
        assert "_blur_payload" in code, "the worker builds the QImage payload"
        assert "QPixmap" not in code, (
            "creating a QPixmap off the GUI thread is undefined behaviour in Qt"
        )

    def test_the_gui_thread_converts_the_payload(self) -> None:
        code = _code(_CANVAS, "collect_warm_backdrop")
        assert "QPixmap.fromImage" in code, "the pixmap is created on the GUI thread"

    def test_the_worker_never_raises(self) -> None:
        """A failed warm leaves the backdrop uncached; the hold then times out."""
        code = _code(_CANVAS, "_warm_worker")
        assert "except Exception" in code
        assert "payload = None" in code

    def test_the_result_crosses_the_boundary_under_a_lock(self) -> None:
        code = _source(_CANVAS)
        assert "self._warm_lock" in code
        assert "with self._warm_lock" in code, "the shared result must be guarded"

    def test_warming_is_idempotent_for_the_same_backdrop(self) -> None:
        """Warm is called every tick, so a repeat must be free."""
        code = _code(_CANVAS, "warm_backdrop")
        assert "self._blur_key == key" in code
        assert "self._warm_key == key" in code, "an in-flight job must not be restarted"

    def test_warming_skips_non_blur_plans(self) -> None:
        code = _code(_CANVAS, "warm_backdrop")
        assert "ambient_strategy != 'blur'" in code


class TestTheBackdropKeyIsShared:
    """Warm, readiness and the render path must agree, or the hold never ends."""

    def test_all_three_use_the_same_key_helper(self) -> None:
        for name in ("warm_backdrop", "backdrop_ready", "_blurred_backdrop"):
            code = _code(_CANVAS, name)
            assert "_backdrop_key(" in code, f"{name} must use the shared key helper"

    def test_the_key_helper_is_deterministic(self) -> None:
        """A key that varied between calls would hold every slide forever."""
        code = _code(_CANVAS, "_backdrop_key")
        assert "id(image)" in code
        assert "plan.screen" in code
        assert "ambient_blur_radius" in code


class TestReadinessIsCheapAndCorrect:
    def test_a_non_blur_plan_is_always_ready(self) -> None:
        code = _code(_CANVAS, "backdrop_ready")
        assert "ambient_strategy != 'blur'" in code
        assert "return True" in code

    def test_readiness_does_no_pixel_work(self) -> None:
        """It runs every frame while a slide is held, so it must be a comparison."""
        code = _code(_CANVAS, "backdrop_ready")
        for expensive in (".scaled(", "_blur_qimage", "_blur_payload", "BoxBlur"):
            assert expensive not in code, f"readiness must not {expensive}"


class TestThePresenterHoldsTheSlide:
    def test_the_hold_checks_the_backdrop_not_just_the_plan(self) -> None:
        """The core requirement: no transition until the blur has finished."""
        code = _code(_PRESENTER, "_transition_ready")
        assert "ambient_strategy != 'blur'" in code
        assert "backdrop_ready" in code, "the backdrop must gate the transition"

    def test_the_advance_path_uses_the_readiness_test(self) -> None:
        code = _code(_PRESENTER, "render")
        assert "_transition_ready()" in code, "advance must be gated on readiness"

    def test_a_missing_backend_method_does_not_block(self) -> None:
        """A backend with no backdrop support must not hold slides forever."""
        code = _code(_PRESENTER, "_transition_ready")
        # ``ast.unparse`` normalises quotes, so match on the call shape.
        assert "getattr(self._backend, 'backdrop_ready', None)" in code
        assert "return True" in code

    def test_readiness_uses_the_non_blocking_cache_lookup(self) -> None:
        """``_image_for`` loads synchronously on a miss — the exact stall to avoid.

        This runs every frame while a slide is held, so a blocking load here
        would put the stall back in a new place.
        """
        code = _code(_PRESENTER, "_transition_ready")
        assert "_cache.get(" in code
        assert "_image_for(" not in code, "readiness must not use the blocking accessor"

    def test_the_warm_call_uses_the_non_blocking_cache_lookup(self) -> None:
        code = _code(_PRESENTER, "render")
        assert "_cache.get(" in code
        assert "_image_for(" not in code, "the per-tick warm must not use the blocking accessor"

    def test_the_result_is_collected_every_tick(self) -> None:
        """Warming is useless if the finished backdrop is never adopted."""
        code = _code(_PRESENTER, "render")
        assert "collect_warm_backdrop" in code

    def test_warming_happens_while_the_slide_is_showing(self) -> None:
        """Warm is started from render(), before any transition begins."""
        code = _code(_PRESENTER, "render")
        assert "warm_backdrop" in code
        warm_at = code.index("warm_backdrop")
        advance_at = code.index("_advance(initial=False)")
        assert warm_at < advance_at, "the warm must be issued before the advance"


class TestTheBackendForwardsTheCalls:
    def test_the_backend_exposes_all_three(self) -> None:
        source = _source(_BACKEND)
        for name in ("def backdrop_ready", "def warm_backdrop", "def collect_warm_backdrop"):
            assert name in source, f"the presenter only talks to the backend: {name}"

    def test_a_missing_canvas_is_treated_as_ready(self) -> None:
        """Otherwise a teardown race would hold a slide forever."""
        code = _code(_BACKEND, "backdrop_ready")
        assert "self._canvas is None" in code
        assert "return True" in code


class TestTheRenderPathDegradesWithoutBlocking:
    def test_an_unready_backdrop_paints_the_flat_colour(self) -> None:
        """The canvas must never build a blur inside a paint."""
        code = _code(_CANVAS, "_draw_backdrop_layer")
        assert "_paint_flat_backdrop" in code, (
            "an unready backdrop must fall back to a flat fill, not block"
        )

    def test_the_draw_layer_does_not_blur(self) -> None:
        code = _code(_CANVAS, "_draw_backdrop_layer")
        assert "_blur_qimage" not in code, "no blur on the paint path"
        assert "BoxBlur" not in code
