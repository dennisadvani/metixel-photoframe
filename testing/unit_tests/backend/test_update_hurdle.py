# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the hardware hurdle on automatic major upgrades.

2.0.0 raised the hardware floor.  The weekly AUTO-update must not install it on
a Pi 2 / Pi 3 / Zero 2 W, because the board would come up unusably slow or not
at all — with no one watching and no chance to intervene.  The manual install
must keep working, since the user can read the changelog and decide.

What matters here:

  * the AUTO path is blocked for a >= 2.0.0 candidate on an older board;
  * the AUTO path is NOT blocked on a Pi 4 / Pi 5;
  * a candidate BELOW the floor is never blocked, whatever the board (a Pi 3
    must keep receiving 1.2.x updates);
  * an undetectable board is treated as incapable (fails safe);
  * ``apply_update`` remains callable — the manual button is unaffected.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any, cast
from unittest import mock

import pytest

from metixel.backend.update_manager import (
    MIN_SAFE_PI_MODELS,
    MIN_SAFE_VERSION,
    UpdateManager,
)


class _FakeConfig:
    """Config stand-in exposing the `updates` section the manager reads.

    A real ``Config.updates`` is a read-only property, so a MagicMock cannot be
    assigned through it in a type-checked way — and a typed fake also lets the
    tests inspect exactly which config writes happened.
    """

    def __init__(self, updates: dict) -> None:
        self.updates = updates


class _FakeState:
    """StateManager stand-in recording `update_config` calls."""

    def __init__(self, updates: dict) -> None:
        self.config = _FakeConfig(updates)
        self.calls: list[tuple[str, dict]] = []

    def update_config(self, section: str, values: dict) -> None:
        self.calls.append((section, values))


def _fake_state(mgr: UpdateManager) -> _FakeState:
    """Return the manager's state as the typed fake installed by _manager().

    ``UpdateManager._state`` is annotated as the real ``StateManager``, so the
    duck-typed stand-in has to be narrowed explicitly rather than fought with
    per-line ignores.
    """
    return cast(_FakeState, mgr._state)


def _manager(model: str | None, channel_version: str | None) -> UpdateManager:
    """Build a bare UpdateManager (no __init__) with a stubbed model + cache."""
    mgr = UpdateManager.__new__(UpdateManager)
    mgr._lock = threading.Lock()
    mgr._state = cast(Any, _FakeState({"channel": "stable"}))
    mgr._cache = {}
    mgr._repo_root = None
    mgr._check_in_progress = False
    mgr._update_in_progress = False
    mgr._last_error = None
    mgr._cache_time = 0.0
    if channel_version is not None:
        mgr._cache["available"] = {
            "stable": {"version": channel_version, "is_newer": True},
        }
    return mgr


def _set_updates(mgr: UpdateManager, values: dict) -> None:
    """Replace the fake config's `updates` section."""
    _fake_state(mgr).config.updates = values


@pytest.fixture
def fake_model(monkeypatch):
    """Patch the model probe used by the hurdle."""

    def _set(model: str | None) -> None:
        import metixel.shared.platform as platform

        monkeypatch.setattr(platform, "detect_pi_model", lambda: model)

    return _set


def _freeze_auto_update_window(monkeypatch) -> None:
    """Make ``datetime.now()`` return a Monday inside the auto-update window.

    ``_maybe_auto_update`` reads the real clock and calls ``.astimezone()``, so
    the frozen value must be built in LOCAL time: a UTC 04:30 would become
    14:30 locally (e.g. AUS Eastern) and the window check would correctly
    reject it.  A ``datetime`` subclass is used (not a MagicMock) so
    ``.replace()``, arithmetic and comparisons behave as production expects.
    """
    import metixel.backend.update_manager as um

    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    local_tz = dt.datetime.now().astimezone().tzinfo
    fixed = dt.datetime(monday.year, monday.month, monday.day, 4, 30, tzinfo=local_tz)

    class _FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return fixed.astimezone(tz) if tz is not None else fixed

    monkeypatch.setattr(um, "datetime", _FixedDateTime)


class TestCapability:
    """Which boards are allowed to auto-upgrade past the floor."""

    def test_pi4_and_pi5_are_capable(self, fake_model) -> None:
        # frozenset(...) is the literal side of the comparison, so ruff's
        # SIM300 expects it on the LEFT (the reverse reads as a Yoda condition).
        assert frozenset({"pi4", "pi5"}) == MIN_SAFE_PI_MODELS

    @pytest.mark.parametrize("model", ["pi4", "pi5"])
    def test_capable_boards_report_hardware_ok(self, fake_model, model) -> None:
        fake_model(model)
        hurdle = UpdateManager._auto_update_hurdle_for("2.0.0")

        assert hurdle["hardware_ok"] is True
        assert hurdle["applies"] is False
        assert hurdle["model"] == model

    @pytest.mark.parametrize("model", ["pi2", "pi3", None])
    def test_older_or_unknown_boards_are_not_capable(self, fake_model, model) -> None:
        """A Pi 3 is 'pi3'; the Zero 2 W collapses to 'pi3' too.

        ``None`` (non-Pi, or an unreadable model file) is deliberately treated
        as incapable — assuming capable would be the dangerous direction.
        """
        fake_model(model)
        hurdle = UpdateManager._auto_update_hurdle_for("2.0.0")

        assert hurdle["hardware_ok"] is False
        assert hurdle["applies"] is True
        # The reason is the complete user-facing sentence naming the cause, the
        # detected board and the floor ("Pi 4 or newer") — the dashboard renders
        # it verbatim, so it must not depend on the JS to make sense.
        assert "pi 4" in hurdle["reason"].lower()
        assert "will not install" in hurdle["reason"].lower()
        assert "2.0.0" in hurdle["reason"]
        if model:
            assert model in hurdle["reason"]


class TestVersionBoundary:
    """The hurdle applies to >= 2.0.0 only."""

    @pytest.mark.parametrize("model", ["pi3", None])
    @pytest.mark.parametrize("version", ["2.0.0", "2.0.1", "2.1.0", "3.0.0", "v2.0.0"])
    def test_at_or_above_floor_is_withheld(self, fake_model, model, version) -> None:
        fake_model(model)

        assert UpdateManager._auto_update_hurdle_for(version)["applies"] is True

    @pytest.mark.parametrize("model", ["pi3", None])
    @pytest.mark.parametrize("version", ["1.2.4", "1.2.5", "1.9.9", "1.12.0"])
    def test_below_floor_is_never_withheld(self, fake_model, model, version) -> None:
        """A Pi 3 must keep receiving 1.2.x/1.x updates automatically.

        Without this the hurdle would freeze every older board at its current
        version — including on security fixes — which is far worse than the
        problem it solves.
        """
        fake_model(model)
        hurdle = UpdateManager._auto_update_hurdle_for(version)

        assert hurdle["applies"] is False
        assert hurdle["reason"] == ""

    def test_no_candidate_is_not_withheld(self, fake_model) -> None:
        fake_model("pi3")
        hurdle = UpdateManager._auto_update_hurdle_for("")

        assert hurdle["applies"] is False
        assert hurdle["candidate"] is None

    def test_floor_is_2_0_0(self) -> None:
        assert MIN_SAFE_VERSION == (2, 0, 0)


class TestAutoUpdatePathIsBlocked:
    """The weekly auto-update must actually refuse to install."""

    def test_pi3_does_not_auto_install_2_0_0(self, fake_model, monkeypatch) -> None:
        """THE regression: the unattended path must not upgrade a Pi 3."""
        fake_model("pi3")
        mgr = _manager("pi3", "2.0.0")
        _set_updates(
            mgr,
            {
                "auto_update": True,
                "auto_update_day": 0,
                "auto_update_time": "04:30",
            },
        )
        _freeze_auto_update_window(monkeypatch)
        applied = mock.MagicMock(return_value={"status": "ok"})
        monkeypatch.setattr(mgr, "apply_update", applied)

        mgr._maybe_auto_update()

        applied.assert_not_called()
        # The weekly stamp must NOT be written: nothing was installed, and
        # stamping would suppress the user's own later window.
        assert not any("last_auto_update" in values for _section, values in _fake_state(mgr).calls)

    def test_pi4_does_auto_install_2_0_0(self, fake_model, monkeypatch) -> None:
        """A capable board must still take the upgrade — otherwise the
        hurdle would silently disable auto-update for everyone."""
        fake_model("pi4")
        mgr = _manager("pi4", "2.0.0")
        _set_updates(
            mgr,
            {
                "auto_update": True,
                "auto_update_day": 0,
                "auto_update_time": "04:30",
            },
        )
        _freeze_auto_update_window(monkeypatch)
        applied = mock.MagicMock(return_value={"status": "ok"})
        monkeypatch.setattr(mgr, "apply_update", applied)

        mgr._maybe_auto_update()

        applied.assert_called_once()

    def test_pi3_still_auto_installs_1_2_x(self, fake_model, monkeypatch) -> None:
        """Below the floor the old behaviour is untouched."""
        fake_model("pi3")
        mgr = _manager("pi3", "1.2.6")
        _set_updates(
            mgr,
            {
                "auto_update": True,
                "auto_update_day": 0,
                "auto_update_time": "04:30",
            },
        )
        _freeze_auto_update_window(monkeypatch)
        applied = mock.MagicMock(return_value={"status": "ok"})
        monkeypatch.setattr(mgr, "apply_update", applied)

        mgr._maybe_auto_update()

        applied.assert_called_once()


class TestManualInstallStaysPossible:
    """The hurdle must never become a hard block."""

    def test_apply_update_is_not_gated_by_the_hurdle(self, fake_model) -> None:
        """``apply_update`` must not consult the hurdle.

        The manual Install button and ``POST /api/updates/apply`` share this
        method.  If the guard lived here the user could never upgrade by hand,
        which is the opposite of what was asked for.
        """
        import inspect

        source = inspect.getsource(UpdateManager.apply_update)

        assert "MIN_SAFE" not in source
        assert "_auto_update_hurdle_for" not in source
        assert "hardware" not in source.lower()


class TestStatusPayload:
    """The dashboard notice is driven by get_status()."""

    def test_status_publishes_the_hurdle(self, fake_model) -> None:
        fake_model("pi3")
        mgr = _manager("pi3", "2.0.0")
        mgr._list_local_releases = lambda: []  # type: ignore[method-assign]
        mgr._current_release = lambda: None  # type: ignore[method-assign]

        status = mgr.get_status()

        hurdle = status["auto_update_hurdle"]
        assert hurdle["applies"] is True
        assert hurdle["candidate"] == "2.0.0"
        assert hurdle["min_safe_version"] == "2.0.0"

    def test_status_on_capable_board_does_not_apply(self, fake_model) -> None:
        fake_model("pi5")
        mgr = _manager("pi5", "2.0.0")
        mgr._list_local_releases = lambda: []  # type: ignore[method-assign]
        mgr._current_release = lambda: None  # type: ignore[method-assign]

        assert mgr.get_status()["auto_update_hurdle"]["applies"] is False
