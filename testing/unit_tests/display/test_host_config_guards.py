# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Host-configuration guards for the display stack.

These assert facts about files that are NOT Python: the systemd units and the
cage launcher.  They exist because the most expensive failures on a wall-mounted
frame come from losing a line of configuration, not from a logic bug — and a
lost line produces a black screen at boot with nothing in the log to explain it.

Each test names the failure it prevents so a future edit that trips one is told
*why* the line is load-bearing rather than just that a string is missing.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CAGE_UNIT = REPO_ROOT / "systemd" / "metixel-cage.service"
BACKEND_UNIT = REPO_ROOT / "systemd" / "metixel-backend.service"
CAGE_LAUNCH = REPO_ROOT / "scripts" / "cage_launch.sh"


class TestQtPlatformPin:
    """The QPA pin is the difference between a loud failure and a black screen."""

    def test_cage_unit_pins_qt_to_wayland(self) -> None:
        """``QT_QPA_PLATFORM=wayland`` must be set for the frontend process.

        Qt chooses its platform plugin at runtime.  Under cage the correct one
        is ``wayland``; auto-detection falls back to ``xcb`` when the Wayland
        plugin is unavailable, and an X11-less frame then renders nothing at
        all.  Pinning makes a missing plugin abort startup instead, which the
        OTA health gate can see and roll back.
        """
        text = CAGE_UNIT.read_text(encoding="utf-8")
        assert "Environment=QT_QPA_PLATFORM=wayland" in text, (
            "metixel-cage.service must pin QT_QPA_PLATFORM=wayland. Without it, "
            "Qt silently falls back to the xcb plugin and the frame shows a "
            "black screen. If you are changing this deliberately, update this "
            "test AND confirm the Wayland plugin still loads on hardware."
        )

    def test_cage_unit_has_exactly_one_qt_platform_value(self) -> None:
        """A second assignment would silently win over the first."""
        lines = [
            ln.strip()
            for ln in CAGE_UNIT.read_text(encoding="utf-8").splitlines()
            if ln.strip().startswith("Environment=QT_QPA_PLATFORM")
        ]
        assert len(lines) == 1, f"expected one QT_QPA_PLATFORM line, found {lines}"


class TestRetiredPi3dPlumbing:
    """The pi3d-era env switch is gone from the units."""

    def test_units_do_not_set_the_backend_override(self) -> None:
        """``METIXEL_DISPLAY_BACKEND`` was a pi3d factory switch.

        It is still supported as a desktop dev override, but nothing in the
        deployment should set it: an ``auto`` value that no longer selects
        anything hides which backend is really running.
        """
        for unit in (CAGE_UNIT, BACKEND_UNIT):
            text = unit.read_text(encoding="utf-8")
            assert "METIXEL_DISPLAY_BACKEND" not in text, (
                f"{unit.name} must not set METIXEL_DISPLAY_BACKEND — it was a "
                f"pi3d factory switch and is now a desktop-only dev override."
            )


class TestPhantomOutputRationale:
    """The phantom-output fix must stay explained on its own terms.

    Its comment used to attribute the cleanup to pi3d needing an X11 surface.
    That is wrong, and it is dangerous: a future change that removes XWayland
    or pi3d references could reasonably delete the whole block, reintroducing
    the aspect-ratio distortion it prevents.
    """

    def test_launcher_still_disables_phantom_outputs(self) -> None:
        text = CAGE_LAUNCH.read_text(encoding="utf-8")
        assert "wlr-randr" in text, "cage_launch.sh must still disable phantom outputs"
        assert "--off" in text, "phantom outputs are disabled with `wlr-randr --output X --off`"

    def test_launcher_explains_the_aspect_ratio_reason(self) -> None:
        text = CAGE_LAUNCH.read_text(encoding="utf-8")
        lower = text.lower()
        assert "aspect" in lower, (
            "cage_launch.sh must explain that phantom output cleanup exists for "
            "correct aspect ratio — that is the real reason, and the reason a "
            "future reader must not delete it."
        )

    def test_launcher_does_not_claim_it_is_about_x11(self) -> None:
        """The block is a compositor concern, not an X11 one."""
        text = CAGE_LAUNCH.read_text(encoding="utf-8")
        assert "cage's XWayland\n# root window spans" not in text, (
            "cage_launch.sh must not re-attribute the phantom-output cleanup to "
            "XWayland — the cleanup would be required regardless of the toolkit."
        )
