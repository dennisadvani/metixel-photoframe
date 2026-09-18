# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Guards for the throttled ambient-blur subprocess and its sequencing.

The defect these exist for: with the ``blur`` ambient fill selected, playback was
choppy and occasionally stalled.  The blurred backdrop was being built inside the
frontend process — a Pillow filter plus a ``QImage -> PNG -> PIL -> PNG ->
QImage`` round trip, tens of milliseconds of CPU on a machine that is also
decoding photos and driving a 30 fps panel.

Three properties carry the fix, and all three need guarding because any one of
them alone is useless:

1. **The work is throttled.**  It runs in a child process wrapped in ``nice``
   and, where installed, ``cpulimit``.  Those are PROCESS-level, which is why a
   worker thread is not an option — it would be the same stall with nothing
   capping it.
2. **It happens off the transition.**  The next item's backdrop is built during
   the CURRENT slide; nothing is started, and nothing is loaded, while a
   crossfade is on screen.
3. **The screen waits for it.**  The boot screen stays up until the first
   backdrop is loaded, and a transition is held until the next one is.  Without
   this the work is merely deferred and the flat band still flickers.

Identity matters as much as the plumbing: the source file, its fingerprint, the
target size and the radius are all part of what a backdrop IS, so a change to
any of them can never be served a stale image.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Any

import pytest

from metixel.display.ambient_blur import BackdropRequest, BackdropRunner

_ROOT = Path(__file__).resolve().parents[3]
_BLUR = _ROOT / "src" / "metixel" / "display" / "ambient_blur.py"
_CANVAS = _ROOT / "src" / "metixel" / "display" / "qt_canvas.py"
_BACKEND = _ROOT / "src" / "metixel" / "display" / "qt_backend.py"
_PRESENTER = _ROOT / "src" / "metixel" / "frontend" / "presentation" / "presenter.py"
_RENDERER = _ROOT / "src" / "metixel" / "frontend" / "renderer.py"


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


# ---------------------------------------------------------------------------
# Fixtures: a runner whose child never really spawns
# ---------------------------------------------------------------------------


class _FakeProc:
    """Stands in for ``subprocess.Popen``.  Records the command, runs nothing."""

    def __init__(self, cmd: list[str], **kwargs: Any) -> None:
        self.cmd = cmd
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


class _Spawner:
    """Captured spawns and the signals sent to them."""

    def __init__(self) -> None:
        self.procs: list[_FakeProc] = []
        self.signals: list[tuple[int, int]] = []


@pytest.fixture
def spawner(monkeypatch: pytest.MonkeyPatch) -> _Spawner:
    """Replace the child process and the process-group signals sent to it.

    ``os.killpg`` is patched as well as ``Popen``: the fake pid is arbitrary, and
    a real signal to whatever process happens to own it is not something a test
    is allowed to risk.
    """
    captured = _Spawner()

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeProc:
        proc = _FakeProc(cmd, **kwargs)
        captured.procs.append(proc)
        return proc

    monkeypatch.setattr("metixel.display.ambient_blur.subprocess.Popen", fake_popen)
    monkeypatch.setattr("metixel.display.ambient_blur.os.getpgid", lambda pid: pid)
    monkeypatch.setattr(
        "metixel.display.ambient_blur.os.killpg",
        lambda pgid, sig: captured.signals.append((pgid, sig)),
    )
    return captured


@pytest.fixture
def runner(tmp_path: Path) -> BackdropRunner:
    return BackdropRunner(tmp_dir=tmp_path / "ambient")


def _request(
    tmp_path: Path,
    name: str = "a.jpg",
    *,
    width: int = 1920,
    height: int = 1200,
    radius: float = 24.0,
    blur_filter: object = None,
) -> BackdropRequest:
    """A real request, so the file fingerprint in its identity is real too."""
    source = tmp_path / name
    if not source.exists():
        source.write_bytes(b"pretend jpeg")
    request = BackdropRequest.build(source, width, height, radius, blur_filter)
    assert request is not None
    return request


def _throttled(monkeypatch: pytest.MonkeyPatch, *, cpulimit: bool = True) -> None:
    """Pretend the throttling binaries are installed."""
    found = {"/usr/bin/nice", "/usr/bin/cpulimit"} if cpulimit else {"/usr/bin/nice"}
    monkeypatch.setattr(
        "metixel.shared.throttle.shutil.which",
        lambda name: f"/usr/bin/{name}" if f"/usr/bin/{name}" in found else None,
    )


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class TestTheBackdropIdentity:
    """What a backdrop IS — so a stale one can never be shown for a new one."""

    def test_the_source_is_part_of_it(self, tmp_path: Path) -> None:
        assert _request(tmp_path, "a.jpg").job_id != _request(tmp_path, "b.jpg").job_id

    def test_the_file_fingerprint_is_part_of_it(self, tmp_path: Path) -> None:
        """Re-optimising a photo in place rewrites the same path."""
        source = tmp_path / "a.jpg"
        source.write_bytes(b"one")
        first = BackdropRequest.build(source, 100, 50, 8.0)
        source.write_bytes(b"changed")
        second = BackdropRequest.build(source, 100, 50, 8.0)

        assert first is not None and second is not None
        assert first.job_id != second.job_id

    def test_the_target_size_is_part_of_it(self, tmp_path: Path) -> None:
        """A resize or rotation invalidates every backdrop."""
        wide = _request(tmp_path, width=1920, height=1200)
        tall = _request(tmp_path, width=1200, height=1920)
        assert wide.job_id != tall.job_id

    def test_the_radius_is_part_of_it(self, tmp_path: Path) -> None:
        """The blur is baked into the pixels, so it cannot be re-used."""
        assert _request(tmp_path, radius=8.0).job_id != _request(tmp_path, radius=60.0).job_id

    def test_the_kernel_is_part_of_it(self, tmp_path: Path) -> None:
        """Same reason as the radius: the chosen filter is baked in too."""
        box = _request(tmp_path, blur_filter="box")
        gaussian = _request(tmp_path, blur_filter="gaussian")
        assert box.job_id != gaussian.job_id
        assert box.blur_filter == "box"
        assert gaussian.blur_filter == "gaussian"

    def test_the_radius_is_clamped_before_it_becomes_identity(self, tmp_path: Path) -> None:
        """A hand-edited config must not be able to produce a different identity
        for a radius the filter would clamp to the same value anyway."""
        huge = _request(tmp_path, radius=1e9)
        assert huge.radius == 100.0
        assert huge.job_id == _request(tmp_path, radius=250.0).job_id

    def test_darkening_is_not_part_of_it(self, tmp_path: Path) -> None:
        """Brightness is a paint-time rect, so it must not invalidate a blur."""
        request = _request(tmp_path)
        fields = set(type(request).__dataclass_fields__)
        assert not {f for f in fields if "darken" in f}, (
            "darken must stay out of the identity, or the slider rebuilds every backdrop"
        )

    def test_a_missing_source_has_no_identity(self, tmp_path: Path) -> None:
        """``None`` is the honest 'nothing to build' answer."""
        assert BackdropRequest.build(tmp_path / "nope.jpg", 100, 50, 8.0) is None
        assert BackdropRequest.build(None, 100, 50, 8.0) is None

    def test_the_identity_is_stable_across_calls(self, tmp_path: Path) -> None:
        """A volatile identity would hold every slide forever."""
        source = tmp_path / "a.jpg"
        source.write_bytes(b"same")
        first = BackdropRequest.build(source, 100, 50, 8.0)
        second = BackdropRequest.build(source, 100, 50, 8.0)
        assert first == second


# ---------------------------------------------------------------------------
# The subprocess
# ---------------------------------------------------------------------------


class TestTheSubprocessIsThrottled:
    """``nice`` and ``cpulimit`` are the reason this is a process, not a thread."""

    def test_the_command_is_wrapped_with_nice_and_cpulimit(
        self, runner: BackdropRunner, spawner: _Spawner, tmp_path: Path, monkeypatch
    ) -> None:
        _throttled(monkeypatch)
        runner.start(_request(tmp_path))

        cmd = spawner.procs[0].cmd
        assert cmd[0].endswith("cpulimit")
        assert cmd[cmd.index("-l") + 1] == "50", "half a core, leaving the rest to the renderer"
        assert "-f" in cmd, "cpulimit must own the child's lifetime"
        assert cmd[cmd.index("--") + 1 : cmd.index("--") + 4] == ["nice", "-n", "19"]

    def test_the_command_runs_the_blur_module_with_the_parameters(
        self, runner: BackdropRunner, spawner: _Spawner, tmp_path: Path, monkeypatch
    ) -> None:
        _throttled(monkeypatch)
        request = _request(tmp_path, width=800, height=600, radius=12.0)
        runner.start(request)

        cmd = spawner.procs[0].cmd
        assert cmd[cmd.index("-m") + 1] == "metixel.display.ambient_blur"
        assert str(request.source) in cmd
        assert cmd[cmd.index("--width") + 1] == "800"
        assert cmd[cmd.index("--height") + 1] == "600"
        assert cmd[cmd.index("--radius") + 1] == "12.0"
        assert cmd[cmd.index("--filter") + 1] == "box", "the default kernel is explicit"
        assert cmd[cmd.index("--dest") + 1] == str(runner.output_path(request.job_id))

    def test_the_kernel_is_forwarded_to_the_worker(
        self, runner: BackdropRunner, spawner: _Spawner, tmp_path: Path, monkeypatch
    ) -> None:
        _throttled(monkeypatch)
        runner.start(_request(tmp_path, blur_filter="gaussian"))
        assert spawner.procs[0].cmd[spawner.procs[0].cmd.index("--filter") + 1] == "gaussian"

    def test_nice_still_applies_without_cpulimit(
        self, runner: BackdropRunner, spawner: _Spawner, tmp_path: Path, monkeypatch
    ) -> None:
        """``cpulimit`` is optional; ``nice`` is the floor."""
        _throttled(monkeypatch, cpulimit=False)
        runner.start(_request(tmp_path))

        cmd = spawner.procs[0].cmd
        assert cmd[:3] == ["nice", "-n", "19"]
        assert "cpulimit" not in cmd

    def test_the_child_output_is_discarded(
        self, runner: BackdropRunner, spawner: _Spawner, tmp_path: Path, monkeypatch
    ) -> None:
        """``cpulimit`` prints its own lines to stdout.

        That is not hypothetical: it corrupted a worker's JSON result once, so the
        exit code is the only channel the parent trusts.
        """
        _throttled(monkeypatch)
        runner.start(_request(tmp_path))

        kwargs = spawner.procs[0].kwargs
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL

    def test_the_child_is_its_own_session(
        self, runner: BackdropRunner, spawner: _Spawner, tmp_path: Path, monkeypatch
    ) -> None:
        """So superseding it can kill ``cpulimit`` AND the Python child it runs."""
        _throttled(monkeypatch)
        runner.start(_request(tmp_path))
        assert spawner.procs[0].kwargs["start_new_session"] is True


class TestTheRunnerNeverBlocks:
    """The render loop calls into this every frame; it must never wait."""

    def test_starting_does_not_wait(self) -> None:
        code = _code(_BLUR, "start")
        assert ".communicate(" not in code
        assert ".wait(" not in code

    def test_nothing_is_reported_while_the_child_runs(
        self, runner: BackdropRunner, spawner: _Spawner, tmp_path: Path, monkeypatch
    ) -> None:
        _throttled(monkeypatch)
        runner.start(_request(tmp_path))
        spawner.procs[0].returncode = None
        assert runner.take_finished() is None

    def test_a_finished_job_reports_its_output(
        self, runner: BackdropRunner, spawner: _Spawner, tmp_path: Path, monkeypatch
    ) -> None:
        _throttled(monkeypatch)
        request = _request(tmp_path)
        runner.start(request)

        output = runner.output_path(request.job_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"jpeg")
        spawner.procs[0].returncode = 0

        assert runner.take_finished() == (request.job_id, output)

    def test_a_nonzero_exit_is_a_failure(
        self, runner: BackdropRunner, spawner: _Spawner, tmp_path: Path, monkeypatch
    ) -> None:
        _throttled(monkeypatch)
        request = _request(tmp_path)
        runner.start(request)
        output = runner.output_path(request.job_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"partial")
        spawner.procs[0].returncode = 1

        job_id, path = runner.take_finished() or ("", None)
        assert job_id == request.job_id
        assert path is None
        assert not output.exists(), "a failed job must not leave a partial backdrop behind"

    def test_a_timeout_is_a_failure_rather_than_a_hang(
        self, spawner: _Spawner, tmp_path: Path, monkeypatch
    ) -> None:
        """A wedged child must never hold a slide; it degrades to the flat fill."""
        _throttled(monkeypatch)
        runner = BackdropRunner(tmp_dir=tmp_path / "ambient", timeout_s=0.0)
        request = _request(tmp_path)
        runner.start(request)
        spawner.procs[0].returncode = None

        assert runner.take_finished() == (request.job_id, None)
        assert spawner.signals, "the wedged child must be killed, not just abandoned"

    def test_a_second_request_supersedes_the_first(
        self, runner: BackdropRunner, spawner: _Spawner, tmp_path: Path, monkeypatch
    ) -> None:
        """One backdrop at a time, like the image cache."""
        _throttled(monkeypatch)
        first = _request(tmp_path, "a.jpg")
        second = _request(tmp_path, "b.jpg")
        runner.start(first)
        runner.start(second)

        assert len(spawner.procs) == 2
        assert spawner.signals, "the superseded child must be stopped"
        assert runner.job_id == second.job_id

    def test_the_same_request_is_not_restarted(
        self, runner: BackdropRunner, spawner: _Spawner, tmp_path: Path, monkeypatch
    ) -> None:
        """Warm is called every tick, so a repeat must be free."""
        _throttled(monkeypatch)
        request = _request(tmp_path)
        runner.start(request)
        runner.start(request)
        assert len(spawner.procs) == 1

    def test_release_deletes_the_output(self, runner: BackdropRunner, tmp_path: Path) -> None:
        """tmpfs holds the file only until the caller has it in memory."""
        output = runner.output_path("deadbeef")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"jpeg")
        runner.release("deadbeef")
        assert not output.exists()

    def test_close_stops_the_child(
        self, runner: BackdropRunner, spawner: _Spawner, tmp_path: Path, monkeypatch
    ) -> None:
        """The child is its own session, so it would outlive the frontend."""
        _throttled(monkeypatch)
        runner.start(_request(tmp_path))
        spawner.procs[0].returncode = None
        runner.close()
        assert spawner.signals
        assert runner.job_id is None

    def test_the_runner_imports_no_qt(self) -> None:
        """It runs in the frontend, but nothing here needs a GUI."""
        assert "PySide6" not in _source(_BLUR)


# ---------------------------------------------------------------------------
# Readiness and the screen hold
# ---------------------------------------------------------------------------


class TestTheScreenWaitsForTheBackdrop:
    """Deferring the work is useless if the transition starts without it."""

    def test_readiness_does_no_pixel_work(self) -> None:
        """It runs every frame while a slide is held, so it must be a comparison."""
        code = _code(_CANVAS, "backdrop_ready")
        for expensive in (".scaled(", "BoxBlur", "Image.open", "QPixmap"):
            assert expensive not in code, f"readiness must not do {expensive}"

    def test_a_non_blur_plan_is_always_ready(self) -> None:
        code = _code(_CANVAS, "backdrop_ready")
        assert "ambient_strategy != 'blur'" in code
        assert "return True" in code

    def test_a_missing_source_is_ready_rather_than_held(self) -> None:
        """There is nothing to wait for; holding would be a permanent stall."""
        code = _code(_CANVAS, "backdrop_ready")
        assert "request is None" in code

    def test_a_failed_backdrop_is_ready_rather_than_held(self) -> None:
        code = _code(_CANVAS, "backdrop_ready")
        assert "_backdrop_failed" in code

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
        assert "getattr(self._backend, 'backdrop_ready', None)" in code
        assert "return True" in code

    def test_the_boot_screen_waits_for_the_first_backdrop(self) -> None:
        """The first slide must not appear on a flat band that repaints."""
        code = _code(_RENDERER, "_render_frame")
        assert "slideshow_ready" in code
        assert "ambient_ready" in code, "the boot fade must also require the backdrop"

    def test_the_presenter_answers_the_boot_question(self) -> None:
        code = _code(_PRESENTER, "ambient_ready")
        assert "ambient_strategy != 'blur'" in code
        assert "backdrop_ready" in code
        assert "return True" in code, "a backend without backdrops must not block the fade"

    def test_a_current_item_that_is_not_ready_blocks_the_boot_screen(self) -> None:
        """``ambient_ready`` must be False until the backdrop is loaded."""
        code = _code(_PRESENTER, "ambient_ready")
        assert "self.current_item" in code
        assert "self._current_plan()" in code


# ---------------------------------------------------------------------------
# Sequencing
# ---------------------------------------------------------------------------


class TestBackdropWorkAvoidsTheTransition:
    """Nothing is started, and nothing is loaded, while a crossfade is blending."""

    def test_the_transition_guard_comes_before_any_work(self) -> None:
        code = _code(_PRESENTER, "_service_backdrops")
        assert "_in_transition()" in code
        assert code.index("_in_transition()") < code.index("collect()"), (
            "loading the finished backdrop must not land on a transition frame"
        )

    def test_the_transition_window_is_measured_from_the_slide_clock(self) -> None:
        """A crossfade runs from the slide's duration to duration + transition."""
        code = _code(_PRESENTER, "_in_transition")
        assert "_transition_seconds()" in code
        assert "duration" in code
        assert "_item_start_time" in code

    def test_the_second_backdrop_waits_for_the_boot_fade(self) -> None:
        """The boot screen waits on the first backdrop, so a second job would
        only split the same throttled CPU budget and delay the fade."""
        code = _code(_PRESENTER, "_service_backdrops")
        assert "self._boot_complete" in code
        assert "return" in code

    def test_the_first_backdrop_is_built_during_the_boot_screen(self) -> None:
        """It is requested for the CURRENT item, before the boot gate is reached."""
        code = _code(_PRESENTER, "_service_backdrops")
        current_at = code.index("self._current_plan()")
        boot_at = code.index("self._boot_complete")
        assert current_at < boot_at, "the shown item's backdrop must be requested first"

    def test_the_boot_fade_releases_the_presenter(self) -> None:
        code = _code(_RENDERER, "_render_frame")
        assert "mark_presentation_started()" in code

    def test_a_pipeline_reset_re_arms_the_boot_gate(self) -> None:
        """The boot screen comes back, so it must wait again."""
        code = _code(_RENDERER, "_check_playlist_changed")
        assert "mark_boot_started()" in code

    def test_the_next_backdrop_is_built_from_the_decoded_handle(self) -> None:
        """The slot is keyed by artwork, and the non-blocking lookup is required."""
        code = _code(_PRESENTER, "_request_backdrop")
        assert "_cache.get(" in code
        assert "_image_for(" not in code, "readiness must not use the blocking accessor"

    def test_a_video_backdrop_comes_from_its_poster(self) -> None:
        """ffmpeg is a backend concern; the frontend blurs the frame it shows."""
        code = _code(_PRESENTER, "_backdrop_source")
        assert "first_frame_path" in code
        assert "VIDEO" in code

    def test_the_service_is_a_no_op_for_other_ambient_looks(self) -> None:
        """No per-frame plan work for a slideshow that never blurs."""
        code = _code(_PRESENTER, "_service_backdrops")
        assert code.index("ambient_strategy != 'blur'") < code.index("collect()")


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

    def test_the_child_is_stopped_on_teardown(self) -> None:
        code = _code(_BACKEND, "destroy")
        assert "close_backdrops()" in code
