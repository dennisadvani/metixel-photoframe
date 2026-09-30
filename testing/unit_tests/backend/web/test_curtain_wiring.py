# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""The renderer's curtain handshake, and the routes that request it.

Two halves, and the second is what actually protects the panel:

* the renderer must not run a curtained operation until the screen is *fully*
  black, and must not drop it if the fade never finishes; and
* the routes must ask for the curtain BEFORE scheduling the restart, with a
  delay long enough for the fade to complete.  Asking afterwards, or with too
  short a delay, would run the operation part-way through the fade and show
  exactly the tearing the curtain exists to hide.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parents[4] / "src" / "metixel" / "backend" / "web"
_SYSTEM_ROUTE = _WEB / "routes" / "system.py"
_CONFIG_ROUTE = _WEB / "routes" / "config.py"
_RENDERER = _WEB.parents[1] / "frontend" / "renderer.py"


def _route_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestTheRendererWaitsForBlack:
    """Asserted on the source, because constructing a renderer needs GPU hardware."""

    def _renderer(self) -> str:
        assert _RENDERER.is_file(), f"the renderer is not where expected: {_RENDERER}"
        return _RENDERER.read_text(encoding="utf-8")

    def test_the_curtain_is_registered(self) -> None:
        src = self._renderer()

        assert "CurtainLayer(" in src
        assert "self._overlay.add_layer(self._curtain)" in src

    def test_the_callback_fires_only_when_finished(self) -> None:
        """``finished`` means fully opaque, so this is the ordering guarantee."""
        src = self._renderer()

        match = re.search(r"def _service_curtain\(self\).*?\n    def ", src, re.DOTALL)
        assert match is not None, "_service_curtain is missing"

        body = match.group(0)
        assert "self._curtain.finished" in body, "the operation is not gated on a black screen"
        assert "callback()" in body

    def test_the_callback_is_cleared_before_it_runs(self) -> None:
        """Otherwise an operation that itself curtains would fire twice."""
        src = self._renderer()

        match = re.search(r"def _service_curtain\(self\).*?\n    def ", src, re.DOTALL)
        assert match is not None
        body = match.group(0)

        cleared = body.index("self._curtain_callback = None")
        called = body.index("callback()")
        assert cleared < called, "the callback must be cleared before it is invoked"

    def test_a_failing_operation_releases_the_curtain(self) -> None:
        """A stuck curtain is a black panel with no way back."""
        src = self._renderer()

        match = re.search(r"def _service_curtain\(self\).*?\n    def ", src, re.DOTALL)
        assert match is not None
        body = match.group(0)

        assert "self._curtain.release()" in body, (
            "a raised operation must not leave the screen held black forever"
        )

    def test_a_held_curtain_acts_immediately(self) -> None:
        """A second request during the same black-out must not re-fade."""
        src = self._renderer()

        match = re.search(r"def request_curtain\(.*?\n    def ", src, re.DOTALL)
        assert match is not None
        body = match.group(0)

        assert "is_held" in body, "a repeat request during a black-out would add another fade"

    def test_it_is_driven_from_the_tick(self) -> None:
        """Undriven, the fade would never advance."""
        src = self._renderer()

        assert "self._service_curtain()" in src

    def test_it_degrades_when_there_is_no_overlay(self) -> None:
        """A headless dev run has no overlay; the operation must still happen."""
        src = self._renderer()

        match = re.search(r"def request_curtain\(.*?\n    def ", src, re.DOTALL)
        assert match is not None
        body = match.group(0)

        assert "if then is not None:" in body, (
            "with no overlay the operation must run rather than being silently dropped"
        )


class TestTheShutdownPathHoldsBlack:
    def test_shutdown_holds_black_without_animating(self) -> None:
        """There is no loop left at teardown to drive a fade."""
        src = _RENDERER.read_text(encoding="utf-8")

        assert "hold_black_now(" in src, "shutdown must hold black as the last frame"


class TestTheRoutesCurtainBeforeActing:
    def test_the_curtain_module_exists_and_is_importable(self) -> None:
        from metixel.frontend.overlay.curtain_layer import CurtainLayer

        assert CurtainLayer.Z_CURTAIN < 0.0

    @pytest.mark.parametrize(
        "endpoint",
        ["restart_services", "reboot_system", "shutdown_system"],
    )
    def test_the_power_endpoints_request_the_curtain(self, endpoint: str) -> None:
        src = _route_source(_SYSTEM_ROUTE)

        match = re.search(rf"def {endpoint}\(\)(.*?)(?=\n@|\Z)", src, re.DOTALL)
        assert match is not None, f"{endpoint} not found"

        body = match.group(1)
        assert "_request_curtain(" in body, f"{endpoint} does not curtain the screen"
        assert "_schedule_sudo(" in body

    @pytest.mark.parametrize(
        "endpoint",
        ["restart_services", "reboot_system", "shutdown_system"],
    )
    def test_the_curtain_is_requested_before_the_action(self, endpoint: str) -> None:
        """Order is the whole point: black first, then act."""
        src = _route_source(_SYSTEM_ROUTE)

        match = re.search(rf"def {endpoint}\(\)(.*?)(?=\n@|\Z)", src, re.DOTALL)
        assert match is not None
        body = match.group(1)

        curtain_at = body.index("_request_curtain(")
        schedule_at = body.index("_schedule_sudo(")
        assert curtain_at < schedule_at, (
            "the curtain must be requested BEFORE the action is scheduled, or the "
            "operation runs while the screen is still showing content"
        )

    @pytest.mark.parametrize(
        "endpoint",
        ["restart_services", "reboot_system", "shutdown_system"],
    )
    def test_the_delay_leaves_time_for_the_fade(self, endpoint: str) -> None:
        """Undershooting the fade shows the tearing the curtain was added to hide."""
        from metixel.shared.timing import CURTAIN_FADE_SECONDS, CURTAIN_SETTLE_SECONDS

        src = _route_source(_SYSTEM_ROUTE)

        match = re.search(rf"def {endpoint}\(\)(.*?)(?=\n@|\Z)", src, re.DOTALL)
        assert match is not None
        body = match.group(1)

        assert "delay=CURTAIN_SETTLE_SECONDS" in body, (
            f"{endpoint} uses a delay that cannot be trusted"
        )
        assert CURTAIN_SETTLE_SECONDS > CURTAIN_FADE_SECONDS

    def test_the_route_delay_is_not_a_second_literal(self) -> None:
        """The route must consume the shared constant, not restate a number.

        A duplicated duration drifts from the fade silently, and the failure looks
        like success: the action lands a few frames into the ramp, tearing the
        picture with nothing in the log to say so.
        """
        src = _route_source(_SYSTEM_ROUTE)

        assert "from metixel.shared.timing import CURTAIN_SETTLE_SECONDS" in src, (
            "the settle delay must come from the shared timing module"
        )

    def test_the_routes_do_not_import_the_frontend(self) -> None:
        """The dependency direction must not invert.

        The curtain lives in the frontend, but importing it from a backend route
        would pull the whole overlay stack into the backend's import graph for the
        sake of one float.  The shared timing module exists precisely to avoid
        that.
        """
        for source in (_SYSTEM_ROUTE, _CONFIG_ROUTE):
            text = _route_source(source)
            assert "from metixel.frontend" not in text, (
                f"{source.name} imports the frontend package; the shared constant "
                "belongs in metixel.shared.timing"
            )

    def test_the_config_route_curtains_a_pipeline_rebuild(self) -> None:
        """A rotation reconfigures DRM under the window; a rebuild tears the picture."""
        src = _route_source(_CONFIG_ROUTE)

        assert (
            'ControlMessage(\n                            cmd="curtain"' in src
            or 'cmd="curtain"' in src
        ), "the pipeline-rebuild path does not request the curtain"

    def test_the_config_curtain_precedes_the_restart(self) -> None:
        src = _route_source(_CONFIG_ROUTE)

        # Anchored on the branch and the following dedented statement, because the
        # branch body contains blank lines that a naive ``.*?\n\n`` would stop at.
        match = re.search(r"if needs_rebuild:(.*?)\n(?=\s{8}\S|\s{4}\S|\Z)", src, re.DOTALL)
        assert match is not None, "the needs_rebuild branch was not found"
        body = match.group(1)

        assert 'cmd="curtain"' in body, "the rebuild path does not request the curtain"
        curtain_at = body.index('cmd="curtain"')
        schedule_at = body.index("schedule_sudo(")
        assert curtain_at < schedule_at, (
            "the curtain must be requested before the rebuild restart is scheduled"
        )
