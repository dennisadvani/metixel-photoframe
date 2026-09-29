# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Guards for config hot reload: it must fire, and the change must be visible.

Regression these exist for: saving the slideshow card appeared to do nothing.
Every layer was correct — the template had the control, the JS sent the key, the
route persisted it, and the presenter honoured it — but the running frontend
never reloaded the file, so the frame kept the configuration it booted with.

Two independent faults caused that, so there are two groups of tests here:

1. **Detection.** ``_check_config_changed`` compared float-second mtimes with
   ``>`` against a baseline captured at startup, so any change whose mtime was not
   strictly greater was dropped.  It dropped it *silently* — there was no branch
   that logged a skip — which is what made a wiring bug look plausible.  The
   comparison is now ``!=`` on ``st_mtime_ns``, and both outcomes are logged.

2. **Visibility.** ``Presenter.reload_config`` cleared the cached plans, but
   nothing repainted, so a fit change landed whenever the slide clock next
   happened to tick.  ``represent()`` repaints immediately.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from metixel.frontend.renderer import FrontendRenderer
from metixel.shared.config import DEFAULT_CONFIG


def _write_config(path: Path, fit_mode: str = "cover") -> None:
    """Write a config atomically, the way StateManager does."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["slideshow"]["fit_mode"] = fit_mode
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg), encoding="utf-8")
    tmp.replace(path)


@pytest.fixture
def renderer(tmp_path):
    """A renderer with no backend/presenter, enough to exercise detection."""
    config_path = tmp_path / "config.json"
    _write_config(config_path)

    with mock.patch.object(FrontendRenderer, "_start_heartbeat", lambda self: None):
        inst = FrontendRenderer(config_path, backend=mock.MagicMock())
    inst._presentation = mock.MagicMock()
    return inst


class TestChangeDetectionIsExact:
    def test_nanosecond_mtime_is_used(self, renderer) -> None:
        """Float seconds can compare equal for two saves; st_mtime_ns cannot."""
        stamp = renderer._get_config_mtime_ns()
        assert isinstance(stamp, int)
        assert stamp > 0

    def test_a_newer_file_is_detected(self, renderer, tmp_path) -> None:
        renderer._config_mtime_ns = renderer._get_config_mtime_ns()

        _write_config(tmp_path / "config.json", fit_mode="contain")
        # Force a distinct nanosecond stamp so the test does not depend on the
        # filesystem's timestamp granularity.
        renderer._config_mtime_ns -= 1
        renderer._check_config_changed()

        assert renderer._config.slideshow["fit_mode"] == "contain"

    def test_an_unchanged_file_is_not_reloaded(self, renderer) -> None:
        renderer._config_mtime_ns = renderer._get_config_mtime_ns()
        renderer._check_config_changed()
        renderer._presentation.reload_config.assert_not_called()

    def test_an_older_mtime_still_reloads(self, renderer, tmp_path) -> None:
        """The old ``>`` test dropped this; a restore can move mtime backwards.

        The baseline is set into the future and the file is then written, so the
        file's real stamp is necessarily OLDER than the baseline.  ``!=`` reloads;
        ``>`` silently drops it.  Setting the baseline to a future value (rather
        than nudging the file into the past) is what makes this fail under
        mutation — an earlier version of this test nudged the baseline backwards,
        which left the fresh file still newer and so passed for the wrong reason.
        """
        renderer._config_mtime_ns = renderer._get_config_mtime_ns() + 10_000_000

        _write_config(tmp_path / "config.json", fit_mode="contain")
        renderer._check_config_changed()

        renderer._presentation.reload_config.assert_called_once()
        assert renderer._config.slideshow["fit_mode"] == "contain"

    def test_a_missing_file_does_not_reload_or_raise(self, renderer) -> None:
        renderer._config_mtime_ns = renderer._get_config_mtime_ns()
        (renderer._config_path).unlink()

        renderer._check_config_changed()  # must not raise

        renderer._presentation.reload_config.assert_not_called()

    def test_the_reload_is_logged(self, renderer, tmp_path, caplog) -> None:
        """A silent skip is what made this bug take so long to find."""
        renderer._config_mtime_ns = renderer._get_config_mtime_ns()
        _write_config(tmp_path / "config.json", fit_mode="contain")
        renderer._config_mtime_ns -= 1

        with caplog.at_level("INFO", logger="metixel.frontend.renderer"):
            renderer._check_config_changed()

        assert any("hot reloading" in r.message for r in caplog.records)


class TestTheChangeIsVisibleImmediately:
    def test_reload_represents(self, renderer, tmp_path) -> None:
        renderer._config_mtime_ns = renderer._get_config_mtime_ns()
        _write_config(tmp_path / "config.json", fit_mode="contain")
        renderer._config_mtime_ns -= 1

        renderer._check_config_changed()

        renderer._presentation.reload_config.assert_called_once()
        renderer._presentation.represent.assert_called_once()

    def test_reload_applies_a_log_level_change(self, renderer, tmp_path) -> None:
        renderer._config_mtime_ns = renderer._get_config_mtime_ns()
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["system"]["log_level"] = "DEBUG"
        (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        renderer._config_mtime_ns -= 1

        with mock.patch.object(renderer, "_apply_file_log_level") as applied:
            renderer._check_config_changed()

        applied.assert_called_once()
