# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""The Slideshow card's Ambient Fill controls must exist, and must be saved.

The blur backdrop was fully implemented and the presenter honoured it, but the
dashboard had no controls for it at all — the feature was unreachable from the UI.
A control whose id the module looks up and the template does not define is a
``null`` dereference at best and a setting that silently never saves at worst, and
nothing about either shows up on a frame.

The id list is extracted *from the module* rather than written out here, so a
control added on one side and forgotten on the other fails this test instead of
failing on a device.
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[4] / "src" / "metixel" / "backend" / "web"
_TEMPLATE = _WEB / "templates" / "index.html"
_SETTINGS_JS = _WEB / "static" / "js" / "settings-page.js"

#: The config keys the Ambient Fill group owns.
_AMBIENT_KEYS = (
    "ambient_strategy",
    "ambient_color",
    "ambient_blur_radius",
    "ambient_blur_filter",
    "ambient_darken",
)


def _html() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


def _settings_js() -> str:
    return _SETTINGS_JS.read_text(encoding="utf-8")


class TestTheControlsExist:
    def test_every_ambient_id_the_module_looks_up_is_in_the_template(self) -> None:
        js = _settings_js()
        used = set(
            re.findall(r'getElementById\("((?:cfg-)?ambient-[a-z-]+)"\)', js),
        )
        assert used, "the settings module should reference the ambient controls"

        html = _html()
        for element_id in sorted(used):
            assert f'id="{element_id}"' in html, f"{element_id} is looked up but not defined"

    def test_the_three_modes_are_offered(self) -> None:
        html = _html()

        for mode in ("solid", "bars", "blur"):
            assert f'<option value="{mode}">' in html, mode

    def test_both_blur_kernels_are_offered(self) -> None:
        html = _html()

        for kernel in ("box", "gaussian"):
            assert f'<option value="{kernel}">' in html, kernel

    def test_the_slider_bounds_match_the_backend_clamp(self) -> None:
        """Offering a radius the backend silently clamps would be a lie."""
        from metixel.display.ambient_blur import MAX_RADIUS, MIN_RADIUS

        html = _html()
        slider = re.search(r'id="cfg-ambient-blur"[^>]*', html)
        assert slider is not None
        assert f'min="{int(MIN_RADIUS)}"' in slider.group(0)
        assert f'max="{int(MAX_RADIUS)}"' in slider.group(0)


class TestTheValuesAreLoadedAndSaved:
    def test_the_load_path_reads_every_key(self) -> None:
        js = _settings_js()

        for key in _AMBIENT_KEYS:
            assert f"s.{key}" in js, key

    def test_the_save_payload_carries_every_key(self) -> None:
        js = _settings_js()

        for key in _AMBIENT_KEYS:
            assert f"{key}:" in js, key

    def test_the_mode_select_drives_which_controls_are_shown(self) -> None:
        """A visible control that does nothing is worse than a hidden one."""
        js = _settings_js()

        assert "_toggleAmbientColour(ambientStrategy)" in js, "not applied on load"
        assert 'getElementById("cfg-ambient-strategy")?.addEventListener' in js


class TestTheCacheBusterWasBumped:
    def test_the_spa_entry_point_is_versioned(self) -> None:
        """Editing a module without bumping ``?v=`` serves the old one forever."""
        html = _html()

        assert re.search(r'src="/static/js/main\.js\?v=\d+"', html)
        assert re.search(r'href="/static/css/dashboard\.css\?v=\d+"', html)


class TestFitModeOffersOnlyWhatTheEngineCanDraw:
    """The card must not offer a mode the framing engine cannot render.

    ``_FIT_MODE_TO_OVERFLOW`` maps exactly two modes, and there is deliberately no
    third: the engine has no aspect-distorting fit.  A ``fill`` option therefore
    saved a value the backend rejects, logging a warning and silently falling back
    to ``cover`` — so choosing "Fill (stretch)" did nothing at all, which reads as a
    broken control rather than an unsupported one.
    """

    def test_fill_is_not_offered(self) -> None:
        html = _html()

        assert '<option value="fill">' not in html
        assert "stretch" not in html.lower()

    def test_the_options_are_exactly_the_engine_supported_modes(self) -> None:
        from metixel.frontend.presentation.presenter import _FIT_MODE_TO_OVERFLOW

        html = _html()
        offered = set(re.findall(r'id="cfg-fit"[^>]*>(.*?)</select>', html, re.DOTALL)[0].split())
        values = set(re.findall(r'value="([a-z]+)"', " ".join(offered)))

        assert values == set(_FIT_MODE_TO_OVERFLOW), values

    def test_the_ambient_group_is_hidden_in_cover_mode(self) -> None:
        """Cover is full-bleed, so there is no letterbox for a fill to show in.

        The group has to be a single container the caller can hide, and it must be
        hidden on load as well as on change — otherwise opening the page in cover
        mode still offers controls over nothing.
        """
        html = _html()
        js = _settings_js()

        assert 'id="ambient-group"' in html, "the ambient rows need one container"
        assert '_toggleAmbientGroup(document.getElementById("cfg-fit").value)' in js
        assert 'getElementById("cfg-fit")?.addEventListener' in js

    def test_every_ambient_row_is_inside_the_group(self) -> None:
        """A row left outside the container would stay visible in cover mode."""
        html = _html()
        group = re.search(r'<div id="ambient-group">(.*?)<!-- /ambient-group -->', html, re.DOTALL)

        assert group is not None, "the ambient group is not delimited"
        for element_id in (
            "cfg-ambient-strategy",
            "cfg-ambient-color",
            "cfg-ambient-blur",
            "cfg-ambient-blur-filter",
            "cfg-ambient-darken",
        ):
            assert f'id="{element_id}"' in group.group(1), element_id


class TestSmartCoverIsHiddenInLetterboxMode:
    """Smart Cover only decides *which orientation is cropped*.

    In "Contain (letterbox)" nothing is cropped, so the setting cannot affect the
    frame — it was a live control that silently did nothing.  It is now hidden
    there.

    Hidden rather than disabled on purpose: the checkbox keeps its value and the
    save still writes it, so switching back to Cover restores the user's choice
    instead of silently resetting it to the default.
    """

    def test_the_row_has_an_id_the_module_can_target(self) -> None:
        html = _html()

        assert 'id="smart-cover-row"' in html, "the row needs an addressable container"
        assert 'id="cfg-smart-cover"' in html, "the checkbox itself must survive"

    def test_the_module_toggles_it_on_load_and_on_change(self) -> None:
        """Both are required: a change handler alone leaves the initial state wrong.

        Opening the page in letterbox mode must not show the row while waiting for
        the user to touch the fit-mode select.
        """
        js = _settings_js()

        assert "_toggleSmartCoverRow(" in js, "the toggle helper is missing"
        assert '_toggleSmartCoverRow(document.getElementById("cfg-fit").value)' in js, (
            "the row is not hidden on load"
        )
        assert "_toggleSmartCoverRow(this.value)" in js, "the row does not follow the select"

    def test_it_is_hidden_in_contain_and_shown_in_cover(self) -> None:
        """The direction matters — the condition is the opposite of the ambient one."""
        js = _settings_js()
        match = re.search(
            r"function _toggleSmartCoverRow\(fitMode\)\s*\{(.*?)\n    \}", js, re.DOTALL
        )
        assert match is not None, "_toggleSmartCoverRow is not defined as expected"

        body = match.group(1)
        assert 'fitMode === "cover"' in body, (
            "Smart Cover must be shown in cover mode (where it crops) and hidden in "
            "letterbox mode (where it does not)"
        )

    def test_the_checkbox_is_not_disabled_by_the_hiding(self) -> None:
        """Disabling would stop the value being read back on save."""
        js = _settings_js()
        match = re.search(
            r"function _toggleSmartCoverRow\(fitMode\)\s*\{(.*?)\n    \}", js, re.DOTALL
        )
        assert match is not None
        assert "disabled" not in match.group(1)

    def test_the_value_is_still_saved(self) -> None:
        """Hiding a control must not drop it from the payload."""
        js = _settings_js()

        assert "smart_cover:" in js


class TestTheColourSwatchIsStyledAsASwatch:
    """A colour input is not a text input, and styling it as one renders a dash.

    `input[type="color"]` matched none of the dashboard's shared selectors — not
    `input[type=text]`, not `input[type=range]`, not `input[type=checkbox]` — so it
    fell through to the generic `input:focus` rule and the browser default.  In
    Chrome that default is ~13px of padding around the colour rect, which leaves a
    thin grey sliver in a dark rounded box: the control looked broken while its
    value was entirely correct.  That is why the picker "worked" but the swatch
    appeared not to change.

    `input-premium` was the wrong treatment anyway — it is a text-field style (dark
    fill, placeholder colour), so it cannot show a colour at all.
    """

    def test_the_swatch_uses_its_own_class(self) -> None:
        html = _html()

        swatch = re.search(r'<input[^>]*id="cfg-ambient-color"[^>]*>', html)
        assert swatch is not None, "the ambient colour input is missing"
        assert 'class="swatch-premium"' in swatch.group(0)

    def test_it_is_not_styled_as_a_text_field(self) -> None:
        html = _html()

        swatch = re.search(r'<input[^>]*id="cfg-ambient-color"[^>]*>', html)
        assert swatch is not None
        assert "input-premium" not in swatch.group(0), (
            "input-premium is a text-field treatment and cannot show a colour"
        )

    def test_the_swatch_rule_reaches_the_built_stylesheet(self) -> None:
        """`input.css` is the source; the Pi serves `dashboard.css`.

        Editing `input.css` alone changes nothing in a browser until the Tailwind
        build runs, so this asserts the compiled artefact rather than the source.
        """
        css = (_WEB / "static" / "css" / "dashboard.css").read_text(encoding="utf-8")

        assert "swatch-premium" in css, "run `npm run build:css` — the swatch rule is unbuilt"
        # The specific fix: the inner colour rect only fills the box when the
        # input's padding is zeroed.
        assert "::-webkit-color-swatch-wrapper" in css

    def test_the_stylesheet_cache_buster_moved_with_it(self) -> None:
        """Otherwise the browser serves the previous stylesheet and nothing changes."""
        html = _html()

        assert re.search(r'dashboard\.css\?v=\d+"', html)
