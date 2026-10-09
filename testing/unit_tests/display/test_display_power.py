"""Tests for ``DisplayPower`` tier ordering in ``display/hardware.py``.

These lock down a bug that was invisible to every other test in the suite: on
a single-output cage kiosk ``wlr-randr --off`` exits 0 while changing nothing,
so when it was tried FIRST it short-circuited the working DRM ``status`` write
and the screen simply never turned off.

Evidence (Pi 4, Trixie, vc4-kms-v3d), with one panel attached — the DRM tree
exposes three ``card*-*`` nodes and ALL of them accept the write and report
``rc=0``, but only one does anything:

    card1-HDMI-A-1     status/dpms never move   rc=0  phantom
    card1-Writeback-1  status/dpms never move   rc=0  not a connector
    card1-HDMI-A-2     status->disconnected, dpms->Off  the panel

``glob`` yields the panel LAST.  So an exit code is worthless here: the only
honest success signal is ``dpms`` actually changing, which is what these tests
pin down.  ``dpms`` settles ~50 ms after the write, hence the bounded poll.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from metixel.display.hardware import DisplayPower


class _FakeWlr:
    """Stand-in for ``WlrOutput`` that records calls."""

    def __init__(self, succeed: bool) -> None:
        self.succeed = succeed
        self.calls: list[bool] = []

    def set_power(self, on: bool) -> bool:
        self.calls.append(on)
        return self.succeed


def _connector(
    root: Path,
    name: str,
    *,
    status: str = "connected",
    dpms: str = "On",
    enabled: str = "disabled",
    edid: int = 0,
) -> Path:
    """Create a fake connector node.

    ``status`` is made read-only so the ``sudo tee`` path is exercised, as on
    the Pi where the service runs as ``pi``.

    ``enabled`` defaults to ``disabled`` and ``edid`` to 0 bytes — matching
    the decoy nodes in the real tree.  The live panel needs both
    ``enabled="enabled"`` and non-zero ``edid``, mirroring ``cage_launch.sh``
    (which disables every no-EDID output before the frontend starts).
    """
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    status_file = path / "status"
    status_file.write_text(status)
    status_file.chmod(0o444)
    (path / "dpms").write_text(dpms)
    (path / "enabled").write_text(enabled)
    (path / "edid").write_bytes(b"\x00" * edid)
    return path


def _fake_run_factory(nodes: dict[Path, bool]) -> Any:
    """Return a ``subprocess.run`` stand-in.

    ``nodes`` maps a connector dir to whether it ``follows`` (is the real
    panel).  Only following nodes move ``dpms``; every node returns rc=0,
    exactly as the hardware does — that is the trap being tested.
    """

    def _run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        target = Path(argv[-1])
        for node, follows in nodes.items():
            if target == node / "status":
                if follows:
                    value = str(kwargs.get("input", ""))
                    (node / "dpms").write_text("Off" if value == "off" else "On")
                return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "no such node")

    return _run


@pytest.fixture
def drm_root(tmp_path: Path) -> Path:
    root = tmp_path / "drm"
    root.mkdir()
    return root


class TestTierOrdering:
    """DPMS must be tried before wlr-randr."""

    def test_dpms_is_attempted_before_wlr_randr(self) -> None:
        """The whole point: a lying wlr-randr must not run first.

        If wlr-randr were still first it would return True and DPMS would
        never be reached, reproducing the original bug.
        """
        wlr = _FakeWlr(succeed=True)
        power = DisplayPower(wlr)  # type: ignore[arg-type]

        with patch.object(DisplayPower, "_drm_dpms", return_value=True) as drm:
            power.set(False)

        assert drm.called, "DPMS must be attempted first"
        assert wlr.calls == [], (
            "wlr-randr must NOT be called once DPMS succeeded — it reports "
            "success while changing nothing on a single-output cage kiosk"
        )

    def test_wlr_randr_is_the_fallback_when_dpms_fails(self) -> None:
        """A non-cage Wayland session still has wlr-randr available."""
        wlr = _FakeWlr(succeed=True)
        power = DisplayPower(wlr)  # type: ignore[arg-type]

        with patch.object(DisplayPower, "_drm_dpms", return_value=False):
            power.set(False)

        assert wlr.calls == [False], "wlr-randr fallback should receive 'off'"

    def test_wlr_randr_receives_bool_not_state_string(self) -> None:
        """``WlrOutput.set_power`` takes a bool."""
        wlr = _FakeWlr(succeed=True)
        power = DisplayPower(wlr)  # type: ignore[arg-type]

        with patch.object(DisplayPower, "_drm_dpms", return_value=False):
            power.set(True)

        assert wlr.calls == [True]


class TestConnectorSelection:
    """The panel is chosen by ``enabled``, never by glob order or exit code."""

    def test_phantom_with_lower_glob_order_is_not_chosen(self, drm_root: Path) -> None:
        """The decoy sorts first; the enabled panel must still win.

        ``glob`` yields ``card1-HDMI-A-1`` before ``card1-HDMI-A-2``.  A
        first-match loop writes the phantom, gets rc=0, and leaves the screen
        on — the original bug.
        """
        phantom = _connector(drm_root, "card1-HDMI-A-1")
        panel = _connector(drm_root, "card1-HDMI-A-2", enabled="enabled", edid=256)
        run = _fake_run_factory({phantom: False, panel: True})

        with (
            patch(
                "metixel.display.hardware.glob.glob",
                return_value=[str(phantom), str(panel)],
            ),
            patch("metixel.display.hardware.subprocess.run", side_effect=run),
        ):
            assert DisplayPower._drm_dpms("off") is True

        assert (panel / "dpms").read_text() == "Off"
        assert (phantom / "dpms").read_text() == "On", "phantom must be untouched"

    def test_writeback_node_is_never_chosen(self, drm_root: Path) -> None:
        """An ``enabled=disabled`` node is skipped without hardcoding its name."""
        writeback = _connector(drm_root, "card1-Writeback-1")

        with (
            patch("metixel.display.hardware.glob.glob", return_value=[str(writeback)]),
            patch("metixel.display.hardware.subprocess.run") as run,
        ):
            assert DisplayPower._drm_dpms("off") is False

        run.assert_not_called()

    def test_no_enabled_connector_returns_false(self, drm_root: Path) -> None:
        """All nodes disabled (screen already off) — nothing to act on."""
        _connector(drm_root, "card1-HDMI-A-1")
        _connector(drm_root, "card1-HDMI-A-2")

        with patch(
            "metixel.display.hardware.glob.glob",
            return_value=[
                str(drm_root / "card1-HDMI-A-1"),
                str(drm_root / "card1-HDMI-A-2"),
            ],
        ):
            assert DisplayPower._drm_dpms("off") is False

    def test_enabled_but_no_edid_is_rejected(self, drm_root: Path) -> None:
        """An enabled output with no EDID is a phantom, not a monitor.

        ``cage_launch.sh`` keys off EDID (``disabling phantom output (no
        monitor)``), so that is the underlying fact.  Requiring it here keeps
        the two in agreement even when the launcher did not run.
        """
        phantom = _connector(drm_root, "card1-HDMI-A-1", enabled="enabled", edid=0)

        with (
            patch("metixel.display.hardware.glob.glob", return_value=[str(phantom)]),
            patch("metixel.display.hardware.subprocess.run") as run,
        ):
            assert DisplayPower._drm_dpms("off") is False

        run.assert_not_called()

    def test_reports_false_when_dpms_never_follows(self, drm_root: Path) -> None:
        """Accepting the write is not enough — ``dpms`` must actually move."""
        panel = _connector(drm_root, "card1-HDMI-A-2", enabled="enabled", edid=256)
        run = _fake_run_factory({panel: False})

        with (
            patch("metixel.display.hardware.glob.glob", return_value=[str(panel)]),
            patch("metixel.display.hardware.subprocess.run", side_effect=run),
        ):
            assert DisplayPower._drm_dpms("off") is False


class TestLatchedWakeTarget:
    """``set(True)`` must wake the connector ``set(False)`` switched off.

    Powering a panel off clears ``enabled`` on EVERY node, so the panel can no
    longer be identified by that marker at wake time.  Observed live on a Pi 4
    before this was latched: the wake write landed on the decoy ``HDMI-A-1``,
    reported success, and left the real screen dark.
    """

    def test_off_latches_the_panel_and_on_wakes_it(self, drm_root: Path) -> None:
        phantom = _connector(drm_root, "card1-HDMI-A-1")
        panel = _connector(drm_root, "card1-HDMI-A-2", enabled="enabled", edid=256)
        run = _fake_run_factory({phantom: False, panel: True})

        power = DisplayPower(_FakeWlr(succeed=True))  # type: ignore[arg-type]
        cards = [str(phantom), str(panel)]

        with (
            patch("metixel.display.hardware.glob.glob", return_value=cards),
            patch("metixel.display.hardware.subprocess.run", side_effect=run),
        ):
            power.set(False)

        assert power._powered_off_connector == str(panel)

        # Powering off clears `enabled` AND the EDID on every node — as on the
        # hardware, where the monitor stops driving the DDC lines.
        (panel / "enabled").write_text("disabled")
        (panel / "edid").write_bytes(b"")

        with (
            patch("metixel.display.hardware.glob.glob", return_value=cards),
            patch("metixel.display.hardware.subprocess.run", side_effect=run),
        ):
            power.set(True)

        assert (panel / "dpms").read_text() == "On", "the real panel must be woken"
        assert power._powered_off_connector is None, "latch cleared after waking"

    def test_latch_survives_a_failed_wake(self, drm_root: Path) -> None:
        """A failed wake must not forget which panel to retry."""
        panel = _connector(drm_root, "card1-HDMI-A-2", enabled="enabled", edid=256)
        run = _fake_run_factory({panel: True})
        power = DisplayPower(_FakeWlr(succeed=False))  # type: ignore[arg-type]

        with (
            patch("metixel.display.hardware.glob.glob", return_value=[str(panel)]),
            patch("metixel.display.hardware.subprocess.run", side_effect=run),
        ):
            power.set(False)

        assert power._powered_off_connector == str(panel)

        # Now make every write fail.
        with (
            patch("metixel.display.hardware.glob.glob", return_value=[str(panel)]),
            patch(
                "metixel.display.hardware.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, "", "denied"),
            ),
        ):
            power.set(True)

        assert power._powered_off_connector == str(panel), (
            "the latch must be retained so a later attempt can still wake it"
        )


class TestDirectWrite:
    """On a permissive kernel the direct write is used and sudo is not."""

    def test_direct_write_is_used_when_the_node_is_writable(self, drm_root: Path) -> None:
        panel = drm_root / "card1-HDMI-A-2"
        panel.mkdir(parents=True)
        status = panel / "status"
        status.write_text("connected")
        status.chmod(0o666)
        (panel / "dpms").write_text("On")
        (panel / "enabled").write_text("enabled")
        (panel / "edid").write_bytes(b"\x00" * 256)

        original_open = open

        def _open(path: Any, mode: str = "r", *a: Any, **kw: Any) -> Any:
            handle = original_open(path, mode, *a, **kw)
            if str(path).endswith("/status") and "w" in mode:
                (panel / "dpms").write_text("Off")
            return handle

        with (
            patch("metixel.display.hardware.glob.glob", return_value=[str(panel)]),
            patch("metixel.display.hardware.subprocess.run") as run,
            patch("builtins.open", _open),
        ):
            assert DisplayPower._drm_dpms("off") is True

        run.assert_not_called()
        assert status.read_text() == "off"
