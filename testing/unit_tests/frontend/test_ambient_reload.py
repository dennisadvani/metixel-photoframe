# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Guards for the ambient look surviving a reload.

Regression these exist for: the *fit mode* could be changed from the dashboard
but the *ambient colour* could not, even though both are on the same card, are
saved by the same request, and reach the same ``reload_config``.

The cause was narrower than "settings aren't wired": ``ambient_colour`` and
``ambient_strategy`` are *constructor* arguments of ``LayoutEngine``, while the
fit mode is read per item (``_overflow_for``).  ``reload_config`` rebuilt the
engine only when the rotation changed, so the engine kept the ambient values it
was built with at startup forever, and every plan recomputed from it carried the
old colour.  Fit mode kept working because nothing about it lives in the engine.

So the guard is: after a reload that changes the ambient config, the engine must
reflect it, and the cached plans (which were produced by the old engine) must be
dropped rather than repainted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from metixel.framing.layout import LayoutEngine
from metixel.framing.resolve import MediaSize
from metixel.frontend.presentation.presenter import Presenter
from metixel.shared.config import Config

from .test_presenter import FakeBackend, _image_item


@pytest.fixture
def config() -> Config:
    cfg = Config()
    cfg.update(
        "slideshow",
        {
            "shuffle": False,
            "image_duration_seconds": 30,
            # ``contain`` is what produces an ambient band: it maps to
            # ``overflow="fill"``, which is the only mode where the framing
            # engine derives one.  Under ``cover`` there is no band to colour.
            "fit_mode": "contain",
            "ambient_strategy": "solid",
            "ambient_color": "#101014",
        },
    )
    return cfg


@pytest.fixture
def presenter(config, tmp_path: Path, monkeypatch) -> Presenter:
    monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
    return Presenter(config, FakeBackend())  # type: ignore[arg-type]


class TestTheAmbientEngineIsRebuilt:
    def test_the_engine_starts_with_the_configured_colour(self, presenter: Presenter) -> None:
        assert presenter._layout.ambient_colour == "#101014"

    def test_a_new_colour_rebuilds_the_engine(self, presenter: Presenter) -> None:
        """The regression: this used to stay at the startup value forever."""
        presenter._config.update("slideshow", {"ambient_color": "#1717d3"})
        presenter.reload_config(presenter._config)

        assert presenter._layout.ambient_colour == "#1717d3"

    def test_a_new_strategy_rebuilds_the_engine(self, presenter: Presenter) -> None:
        """The strategy is a constructor argument too, so it had the same fault."""
        presenter._config.update("slideshow", {"ambient_strategy": "bars"})
        presenter.reload_config(presenter._config)

        assert presenter._layout.ambient_strategy == "bars"

    def test_the_new_colour_reaches_a_computed_plan(self, presenter: Presenter) -> None:
        """The engine property is not the contract — the plan is what gets painted."""
        presenter._config.update("slideshow", {"ambient_color": "#1717d3"})
        presenter.reload_config(presenter._config)

        item = _image_item("a", Path("a.jpg"), w=1600, h=1200)
        presenter.set_queue([item])
        plan = presenter._plan_for(MediaSize(item.width, item.height, "image"))

        assert plan.ambient_colour == "#1717d3"
        assert plan.ambient is not None, "a contained 4:3 photo needs an ambient band"

    def test_an_unchanged_ambient_does_not_rebuild_the_engine(self, presenter: Presenter) -> None:
        """Rebuilding on every save would churn the engine for no reason."""
        engine = presenter._layout
        presenter.reload_config(presenter._config)

        assert presenter._layout is engine

    def test_a_rotation_change_still_rebuilds(self, presenter: Presenter) -> None:
        """The pre-existing rotation path must keep working."""
        presenter._config.update("display", {"rotation": 90})
        presenter.reload_config(presenter._config)

        assert presenter._layout.rotation == 90


class TestTheCachedPlansAreDropped:
    def test_an_ambient_change_drops_the_shown_plan(self, presenter: Presenter) -> None:
        """A plan made by the old engine would repaint the old colour."""
        presenter.set_queue([_image_item("a", Path("a.jpg"), w=1600, h=1200)])
        assert presenter._shown_plan is not None

        presenter._config.update("slideshow", {"ambient_color": "#1717d3"})
        presenter.reload_config(presenter._config)

        assert presenter._shown_plan is None

    def test_an_unrelated_change_keeps_the_shown_plan(self, presenter: Presenter) -> None:
        """Dropping the plans on every save would re-run the boot fade."""
        presenter.set_queue([_image_item("a", Path("a.jpg"), w=1600, h=1200)])
        shown = presenter._shown_plan

        presenter._config.update("slideshow", {"image_duration_seconds": 12})
        presenter.reload_config(presenter._config)

        assert presenter._shown_plan is shown


class TestColorNormalisation:
    """``_ambient_colour`` accepts the picker's hex and the legacy rgb list."""

    def test_a_hex_string_passes_through(self) -> None:
        cfg = Config()
        cfg.update("slideshow", {"ambient_color": "#ABCDEF"})
        from metixel.frontend.presentation.presenter import _ambient_colour

        assert _ambient_colour(cfg) == "#abcdef"

    def test_an_rgb_list_is_normalised(self) -> None:
        cfg = Config()
        cfg.update("slideshow", {"ambient_color": [23, 23, 211]})
        from metixel.frontend.presentation.presenter import _ambient_colour

        assert _ambient_colour(cfg) == "#1717d3"

    def test_garbage_falls_back_to_the_default(self) -> None:
        cfg = Config()
        cfg.update("slideshow", {"ambient_color": "not-a-colour"})
        from metixel.frontend.presentation.presenter import _ambient_colour

        assert _ambient_colour(cfg) == "#101014"


class TestEngineExposesItsAmbientState:
    """The reload comparison reads these, so they must not be private-only."""

    def test_the_strategy_property_exists(self) -> None:
        engine = LayoutEngine(ambient_strategy="bars", ambient_colour="#1717d3")
        assert engine.ambient_strategy == "bars"
        assert engine.ambient_colour == "#1717d3"

    def test_defaults_are_none(self) -> None:
        engine = LayoutEngine()
        assert engine.ambient_strategy is None
        assert engine.ambient_colour is None
