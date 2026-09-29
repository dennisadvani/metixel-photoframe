# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the ambient (blurred backdrop) config schema.

The blur itself has been implemented and honoured by the presenter for a while —
what was missing was the *schema*.  The five keys were read with hard-coded
fallbacks and never declared, so ``config.json`` never showed them and the
dashboard had nothing to build a control from: the feature existed and was
unreachable.  These tests pin the declaration, and the agreement between the
declared defaults and the fallbacks the frame actually uses.
"""

from __future__ import annotations

import json

import pytest

from metixel.shared.config import DEFAULT_CONFIG, Config

#: The declared defaults, and the values the presenter falls back to.
_KEYS: dict[str, object] = {
    "ambient_strategy": "solid",
    "ambient_color": "#101014",
    "ambient_blur_radius": 24,
    "ambient_darken": 0.35,
    "ambient_blur_filter": "box",
}


class TestTheKeysAreDeclared:
    def test_every_key_is_in_the_schema(self) -> None:
        for key, value in _KEYS.items():
            assert DEFAULT_CONFIG["slideshow"][key] == value

    def test_a_fresh_config_exposes_them(self) -> None:
        slideshow = Config().slideshow

        for key, value in _KEYS.items():
            assert slideshow[key] == value


class TestUpgradingADevice:
    def test_an_older_config_gains_the_keys(self, tmp_path) -> None:
        """A frame upgrading from a release that predates the blur controls."""
        old = {"slideshow": {"image_duration_seconds": 15, "transition_style": "crossfade"}}
        path = tmp_path / "config.json"
        path.write_text(json.dumps(old), encoding="utf-8")

        config = Config.load(path)

        assert config.slideshow["image_duration_seconds"] == 15
        for key, value in _KEYS.items():
            assert config.slideshow[key] == value

    def test_an_existing_value_survives_a_load(self, tmp_path) -> None:
        """Merging defaults in must not overwrite what the device already chose."""
        custom = {"slideshow": {"ambient_strategy": "blur", "ambient_blur_radius": 61}}
        path = tmp_path / "config.json"
        path.write_text(json.dumps(custom), encoding="utf-8")

        config = Config.load(path)

        assert config.slideshow["ambient_strategy"] == "blur"
        assert config.slideshow["ambient_blur_radius"] == 61


class TestSavingThemRoundTrips:
    """The dashboard saves a whole section, so these travel with the slideshow keys."""

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("ambient_strategy", "blur"),
            ("ambient_color", [10, 20, 30]),
            ("ambient_blur_radius", 60),
            ("ambient_darken", 0.5),
            ("ambient_blur_filter", "gaussian"),
        ],
    )
    def test_the_value_is_persisted(self, tmp_path, key: str, value: object) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps(DEFAULT_CONFIG), encoding="utf-8")

        config = Config.load(path)
        config.update("slideshow", {key: value})
        config.save(path)

        assert Config.load(path).slideshow[key] == value


class TestTheSchemaAgreesWithTheFrame:
    """A schema default and a runtime fallback that disagree is a silent mismatch.

    The dashboard would show one value while the frame painted another, and
    nothing would say which of the two was in play.
    """

    def test_the_runtime_constants_match_the_schema(self) -> None:
        from metixel.display.ambient_blur import DEFAULT_FILTER
        from metixel.frontend.presentation import presenter

        slideshow = DEFAULT_CONFIG["slideshow"]

        assert slideshow["ambient_blur_radius"] == presenter._DEFAULT_AMBIENT_BLUR_RADIUS
        assert slideshow["ambient_darken"] == presenter._DEFAULT_AMBIENT_DARKEN
        assert slideshow["ambient_color"] == presenter._DEFAULT_AMBIENT_COLOUR
        assert slideshow["ambient_blur_filter"] == DEFAULT_FILTER

    def test_an_unusable_value_falls_back_to_the_declared_default(self) -> None:
        """Nonsense must land on the *same* value the schema declares."""
        from metixel.frontend.presentation.presenter import (
            _ambient_blur_filter,
            _ambient_blur_radius,
            _ambient_colour,
            _ambient_darken,
        )

        helpers = {
            "ambient_blur_radius": _ambient_blur_radius,
            "ambient_darken": _ambient_darken,
            "ambient_blur_filter": _ambient_blur_filter,
            "ambient_color": _ambient_colour,
        }

        for key, helper in helpers.items():
            config = Config()
            config.update("slideshow", {key: "nonsense"})

            assert helper(config) == DEFAULT_CONFIG["slideshow"][key], key
