# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the libmpv software-render plumbing.

These matter because the software render API is the *only* thing that stops the
``sync_file`` descriptor leak in the GL render path — the frontend dies of
``OSError: [Errno 24] Too many open files`` after ~27 s of cumulative video
playback without it.  A silent regression here is a device that works until it
plays two videos.

The module is deliberately free of Qt and mpv so it can be tested on a machine
that has neither, which is the build machine.  ``test_sw_render_module_imports_
neither_qt_nor_mpv`` protects that property, and the two guards at the end
protect the widget's use of the API — the part that actually has to stay put.

The size-cap assertions are not arbitrary numbers.  They encode this measured
cost curve (Pi 5, services stopped, 30 fps, ``hwdec=drm-copy``, HEVC), which is
reproduced so the evidence sits next to the assertions:

============ ========= ==========
buffer       of a core ms/frame
============ ========= ==========
476x296       62%       20.7
952x592       80%       26.7
1280x800      82%       27.4
1904x1184    110%       37.0
============ ========= ==========

Splitting by draw rate (30 fps vs 15 fps, same buffer: 13.15 s vs 9.05 s of CPU)
separates a ~30%-of-a-core continuous floor — decode, demux and audio at
realtime — from a ~10 ms fixed plus ~7.4 ms/Mpx cost per ``render()`` call.
"""

from __future__ import annotations

import ast
import ctypes
from pathlib import Path
from typing import Any

import pytest

from metixel.display.sw_render import (
    DEFAULT_SW_FORMAT,
    MIN_SW_DIMENSION,
    SW_FORMATS,
    SW_PARAM_IDS,
    build_sw_params,
    bytes_per_pixel,
    install_sw_render_params,
    sw_target_size,
)
from metixel.shared.platform import sw_render_max_pixels_for_model

_DISPLAY_DIR = Path(__file__).resolve().parents[3] / "src" / "metixel" / "display"


# --------------------------------------------------------------------------
# Buffer sizing
# --------------------------------------------------------------------------


class TestSwTargetSize:
    def test_a_widget_within_budget_is_left_alone(self):
        """No upscaling: a small buffer is a softer picture, not a sharper one."""
        assert sw_target_size(640, 480, 1_000_000) == (640, 480)

    def test_a_widget_exactly_at_budget_is_left_alone(self):
        assert sw_target_size(1000, 1000, 1_000_000) == (1000, 1000)

    @pytest.mark.parametrize(
        ("width", "height", "max_pixels"),
        [
            (1905, 1185, 1_000_000),  # the production artwork rect on a Pi 5
            (1905, 1185, 350_000),  # same, on a Pi 3
            (1920, 1200, 1_000_000),  # the panel itself
            (3840, 2160, 1_000_000),  # 4K media
            (1280, 720, 250_000),
        ],
    )
    def test_the_cap_is_always_respected(self, width: int, height: int, max_pixels: int) -> None:
        """Truncation must never land *above* the budget.

        ``scale`` is chosen so that ``width * scale * height * scale == max``
        exactly; ``int()`` truncates both edges, so the product can only come out
        at or below the cap.  Asserting the inequality rather than an exact
        pixel pair keeps this robust while still catching an off-by-a-factor
        mistake.
        """
        target_w, target_h = sw_target_size(width, height, max_pixels)
        assert target_w * target_h <= max_pixels

    @pytest.mark.parametrize(
        ("width", "height", "max_pixels"),
        [
            (1905, 1185, 1_000_000),
            (1920, 1200, 1_000_000),
            (1280, 720, 350_000),
            (800, 600, 250_000),
        ],
    )
    def test_the_aspect_ratio_is_preserved(self, width: int, height: int, max_pixels: int) -> None:
        """Aspect must survive, or mpv's own panscan/letterbox decision changes.

        mpv applies the fit inside the buffer, so a buffer with the wrong shape
        would crop or letterbox differently from the full-size case — the picture
        would silently disagree with the artwork rect the canvas uses.
        """
        target_w, target_h = sw_target_size(width, height, max_pixels)
        assert target_w / target_h == pytest.approx(width / height, rel=0.01)

    @pytest.mark.parametrize(("width", "height"), [(1905, 1185), (1, 1), (0, 0), (3, 4000)])
    def test_dimensions_never_collapse(self, width: int, height: int) -> None:
        """A zero edge is not a video frame, and would divide by zero."""
        target_w, target_h = sw_target_size(width, height, 1000)
        assert target_w >= MIN_SW_DIMENSION
        assert target_h >= MIN_SW_DIMENSION

    @pytest.mark.parametrize("max_pixels", [0, -1, -1_000_000])
    def test_a_non_positive_cap_means_uncapped(self, max_pixels: int) -> None:
        assert sw_target_size(1905, 1185, max_pixels) == (1905, 1185)

    def test_the_pi5_cap_actually_reduces_the_production_rect(self):
        """The cap must bite, or the frame rate target is not met.

        At the production artwork rect the software path costs 110% of one Pi 5
        core, a 27 fps ceiling.  If this ever returns the untruncated size, the
        cap has silently stopped applying and 30 fps becomes unreachable.
        """
        cap = sw_render_max_pixels_for_model("pi5")
        target_w, target_h = sw_target_size(1905, 1185, cap)
        assert (target_w, target_h) != (1905, 1185)
        assert target_w * target_h <= cap


# --------------------------------------------------------------------------
# Formats
# --------------------------------------------------------------------------


class TestSwFormats:
    @pytest.mark.parametrize(
        ("fmt", "expected"),
        [("rgb0", 4), ("bgr0", 4), ("rgb24", 3), ("rgb565", 2)],
    )
    def test_known_bytes_per_pixel(self, fmt: str, expected: int) -> None:
        assert bytes_per_pixel(fmt) == expected

    def test_the_default_is_a_four_byte_opaque_format(self):
        """``rgb0`` maps 1:1 onto ``QImage.Format_RGBX8888``.

        That pairing is why it is the default: the byte order is explicit rather
        than host-dependent, so Qt uploads it without a conversion pass.
        """
        assert DEFAULT_SW_FORMAT == "rgb0"
        assert SW_FORMATS[DEFAULT_SW_FORMAT] == 4

    def test_an_unknown_format_raises_rather_than_guessing(self):
        """A wrong stride produces a *sheared picture*, not an error.

        Failing loudly at lookup is the only cheap way to catch that, because the
        symptom would otherwise appear as a corrupt image on a device.
        """
        with pytest.raises(KeyError):
            bytes_per_pixel("rgb32")


# --------------------------------------------------------------------------
# Parameter marshalling
# --------------------------------------------------------------------------


def _make_stub_mpvlib() -> Any:  # noqa: ANN401 - a stub module
    """Build a minimal stand-in for the ``mpv`` module.

    ``install_sw_render_params`` only ever touches ``MpvRenderParam.TYPES``, so
    this is faithful and lets the registration be tested without mpv installed.
    Fresh classes per call keep the shared ``TYPES`` dict from leaking between
    tests.
    """

    class _RenderParam:
        TYPES: dict[str, object] = {}

    class _Stub:
        MpvRenderParam = _RenderParam

    return _Stub


class TestInstallSwRenderParams:
    def test_registers_exactly_the_four_parameters_python_mpv_lacks(self):
        """python-mpv 1.0.7's TYPES stops at ``drm_display_v2`` (id 16).

        Without these, ``render(sw_size=...)`` raises ``ValueError: unknown render
        param type`` before libmpv is reached — the software path would create a
        context it could never draw with.
        """
        stub = _make_stub_mpvlib()
        assert stub.MpvRenderParam.TYPES == {}

        install_sw_render_params(stub)

        assert sorted(stub.MpvRenderParam.TYPES) == [
            "sw_format",
            "sw_pointer",
            "sw_size",
            "sw_stride",
        ]

    @pytest.mark.parametrize(
        ("name", "expected_id"),
        [
            ("sw_size", 17),
            ("sw_format", 18),
            ("sw_stride", 19),
            ("sw_pointer", 20),
        ],
    )
    def test_ids_match_libmpv_render_h(self, name: str, expected_id: int) -> None:
        """The ids come from libmpv's ``render.h`` and must not drift.

        A wrong id is not benign: libmpv reads the parameter list positionally by
        type, so a mismatched id would hand it the wrong pointer.
        """
        stub = _make_stub_mpvlib()
        install_sw_render_params(stub)
        assert stub.MpvRenderParam.TYPES[name][0] == expected_id
        assert SW_PARAM_IDS[name] == expected_id

    def test_the_ids_continue_where_python_mpv_stops(self):
        assert sorted(SW_PARAM_IDS.values()) == [17, 18, 19, 20]

    def test_types_are_ones_python_mpv_can_coerce(self):
        """The *type* is half the contract, not just the id.

        python-mpv coerces the caller's value through the registered type:
        ``sw_format`` is a ``char*`` (reuse its ``str`` handler), ``sw_pointer``
        is a ``void*`` (reuse ``c_void_p``), and the two scalars need
        ``Structure`` wrappers because python-mpv only builds a ``Structure``
        from a mapping.
        """
        stub = _make_stub_mpvlib()
        install_sw_render_params(stub)
        types = stub.MpvRenderParam.TYPES

        assert types["sw_format"][1] is str
        assert types["sw_pointer"][1] is ctypes.c_void_p
        for scalar in ("sw_size", "sw_stride"):
            handler = types[scalar][1]
            assert isinstance(handler, type)
            assert issubclass(handler, ctypes.Structure)

    def test_is_idempotent(self):
        """The widget may rebuild its render context after a stop."""
        stub = _make_stub_mpvlib()
        install_sw_render_params(stub)
        first = dict(stub.MpvRenderParam.TYPES)
        install_sw_render_params(stub)
        assert first == stub.MpvRenderParam.TYPES


class TestBuildSwParams:
    def test_stride_is_the_row_pitch_of_the_named_format(self):
        """Stride and format must agree, or the picture is sheared, not errored."""
        params = build_sw_params(64, 32, 0x1234, "rgb565")
        assert params["sw_stride"] == {"value": 64 * 2}
        assert params["sw_size"] == {"width": 64, "height": 32}

    def test_the_format_is_passed_through_verbatim(self):
        assert build_sw_params(16, 8, 0, "rgb24")["sw_format"] == "rgb24"

    def test_the_pointer_is_passed_through_verbatim(self):
        params = build_sw_params(16, 8, 0xDEADBEEF)
        assert isinstance(params["sw_pointer"], ctypes.c_void_p)
        assert params["sw_pointer"].value == 0xDEADBEEF

    def test_every_parameter_is_consumable_by_the_registered_type(self):
        """End-to-end check of the marshalling that would otherwise only fail on a Pi.

        This is the real bug class: the mapping shapes produced here must match
        the ``Structure`` wrappers registered above.  Simulating python-mpv's own
        coercion is the only way to catch a mismatch without hardware.
        """
        stub = _make_stub_mpvlib()
        install_sw_render_params(stub)
        params = build_sw_params(320, 200, 0x2000, "rgb565")

        for name, value in params.items():
            _param_id, handler = stub.MpvRenderParam.TYPES[name]
            if issubclass(handler, ctypes.Structure):
                instance = handler(**value)
                for field, (field_name, _field_type) in zip(
                    value.values(), instance._fields_, strict=True
                ):
                    assert getattr(instance, field_name) == field
            elif handler is ctypes.c_void_p:
                # The value is already a ctypes object, and that is deliberate:
                # the Pi spike proved python-mpv accepts a pre-built c_void_p
                # here, so build_sw_params does not hand it a bare integer.
                assert isinstance(value, ctypes.c_void_p)
                assert value.value == 0x2000
            else:
                assert handler(value) == "rgb565"


# --------------------------------------------------------------------------
# Regression guards for the leak fix itself
# --------------------------------------------------------------------------


def _module_level_imports(path: Path) -> list[str]:
    """Return the module names imported at the top level of *path*.

    Parsed rather than grepped so that a docstring mentioning mpv is not mistaken
    for an import.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


class TestLeakRegressionGuards:
    """These guard the fix, not the module's behaviour.

    The firmware-style guard: the software render API is the only reason video
    playback is usable at all, and reverting it is a one-line change that no
    other test would notice.
    """

    def test_sw_render_module_imports_neither_qt_nor_mpv(self):
        """Keeps the module testable on a machine that has neither.

        This is what lets the buffer-sizing and marshalling logic be covered in
        CI at all — ``qt_mpv`` cannot even be imported without PySide6, so the
        same code living there would be untested.
        """
        imports = _module_level_imports(_DISPLAY_DIR / "sw_render.py")
        for module in imports:
            root = module.split(".")[0]
            assert root not in ("PySide6", "mpv"), f"sw_render must not import {module}"

    def test_widget_uses_the_software_render_api(self):
        """The render context must be ``api_type="sw"``.

        The ``opengl`` API type leaks one ``sync_file`` descriptor per draw call
        on this stack — about 28-30/s, which exhausts a 1024 descriptor limit in
        ~27 s of cumulative playback.  Switching back to it would restore a crash
        that only appears after a video or two.
        """
        source = (_DISPLAY_DIR / "qt_mpv.py").read_text(encoding="utf-8")
        assert 'MpvRenderContext(self._mpv, "sw")' in source

    def test_widget_no_longer_requests_a_gl_framebuffer(self):
        """The GL render arguments are meaningless for the software API.

        Leaving them behind would be a strong hint that someone reverted the
        ``api_type`` without finishing the job.  The check is against identifiers
        that only appear in code — the module docstring *discusses* the retired
        ``get_proc_address`` shim, so matching raw text would fail on its own
        explanation.
        """
        source = (_DISPLAY_DIR / "qt_mpv.py").read_text(encoding="utf-8")
        assert "opengl_fbo" not in source
        assert "flip_y" not in source
        assert "opengl_init_params" not in source
        assert "MpvGlGetProcAddressFn" not in source

    def test_widget_installs_the_missing_render_parameters(self):
        """Registering them is what makes ``render()`` reachable at all."""
        source = (_DISPLAY_DIR / "qt_mpv.py").read_text(encoding="utf-8")
        assert "install_sw_render_params(mpvlib)" in source

    def test_widget_builds_a_fresh_qimage_for_every_frame(self):
        """Caching the QImage freezes the video on a black first texture.

        Qt's GL paint engine caches the uploaded texture keyed by
        ``QImage.cacheKey()``.  For an image built over a raw buffer that key
        changes only when the QImage *object* changes, not when the buffer is
        written — so a single reused QImage is drawn unchanged for ever, and the
        texture it gets stuck on is the black one uploaded before mpv produced its
        first frame.

        This is the exact bug that shipped once and showed up only as a black
        video rectangle with every log healthy, so it is worth a guard against a
        well-meaning "avoid allocating per frame" tidy-up.
        """
        source = (_DISPLAY_DIR / "qt_mpv.py").read_text(encoding="utf-8")
        assert "self._frame_image(" in source
        assert "self._image" not in source
