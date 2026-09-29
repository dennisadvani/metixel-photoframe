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
