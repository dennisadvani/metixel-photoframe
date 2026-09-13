# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for per-board hardware-decode selection.

These encode results measured on real hardware (Pi 5 and Pi 3, services stopped,
1080p HEVC / 720p H.264, CPU read from /proc/stat deltas).  They exist because the
answer is counter-intuitive in three separate ways, and each one would be
expensive to rediscover:

* ``v4l2m2m`` works on a Pi 3 and **not** on a Pi 5;
* ``drm-copy`` works on a Pi 5 and **not** on a Pi 3;
* ``auto`` is wrong on **both** — on a Pi 3 it costs more CPU than requesting no
  hardware decoding at all, because it exhausts CUDA/Vulkan/drm before ever
  reaching the decoder that works.

The mapping must also stay aligned with the transcode profiles: the board whose
profile emits H.265 is the board that can hardware-decode H.265.  If those two
drift, a device transcodes to a codec it cannot decode in hardware — which looks
healthy until the CPU saturates.
"""

from __future__ import annotations

import pytest

from metixel.shared.platform import HWDecByModel, hwdec_for_model

#: Measured CPU for each candidate, as documented in shared/platform.py.
#: Recorded here so the numbers the decision rests on are visible next to it.
MEASURED = {
    "pi5_hevc": {"no": 49.4, "drm-copy": 12.1, "drm": 48.4, "v4l2m2m": 49.2},
    "pi3_h264": {"no": 138.2, "auto": 170.1, "v4l2m2m": 55.4, "drm": 138.9},
}


class TestHwdecMapping:
    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("pi5", "drm-copy"),
            ("pi4", "drm-copy"),
            ("pi3", "v4l2m2m"),
            ("pi2", "v4l2m2m"),
        ],
    )
    def test_known_models_map_to_their_measured_decoder(self, model: str, expected: str) -> None:
        assert hwdec_for_model(model) == expected

    @pytest.mark.parametrize("model", [None, "", "unknown", "radxa-zero-3w"])
    def test_unknown_model_requests_software_explicitly(self, model: str | None) -> None:
        """An unidentified board gets ``"no"``, never ``"auto"``.

        ``auto`` on a Pi 3 measured 170.1% CPU against 138.2% for plain software —
        so guessing ``auto`` is worse than admitting we do not know.  ``"no"`` is
        the honest, cheap answer, and the failure mode is a stutter rather than a
        crash, which is the harder one to diagnose.
        """
        assert hwdec_for_model(model) == "no"

    def test_never_returns_auto(self) -> None:
        """``auto`` must not appear in the mapping at all.

        This is the guard against a well-meaning change "simplifying" the table
        back to `auto` for everything: it is the tempting default, and it is
        measurably the wrong one on both supported boards.
        """
        assert "auto" not in HWDecByModel.values()
        for model in ("pi5", "pi4", "pi3", "pi2", None, "nonsense"):
            assert hwdec_for_model(model) != "auto"


class TestProfilesAgreeWithDecoders:
    """The codec a board transcodes to must be one it can hardware-decode.

    A board that emits H.265 but has no HEVC decoder software-decodes every video
    it produces — visible only as sustained high CPU on the device.
    """

    def test_pi5_profile_codec_has_a_working_decoder(self) -> None:
        from metixel.backend.processing.video import VideoProcessor

        profile = VideoProcessor.PROFILES["pi5"]
        assert profile["codec"] == "h265", (
            "Pi 5 is expected to transcode to H.265 because that is the codec its "
            "hardware decodes (via drm-copy). Changing this to h264 would remove "
            "hardware decode entirely — Pi 5 has NO working H.264 decoder."
        )
        assert hwdec_for_model("pi5") == "drm-copy"

    def test_pi3_profile_codec_has_a_working_decoder(self) -> None:
        from metixel.backend.processing.video import VideoProcessor

        profile = VideoProcessor.PROFILES["pi3"]
        assert profile["codec"] == "h264", (
            "Pi 3 is expected to transcode to H.264 because VC4 has no HEVC "
            "decoder; HEVC software-decodes at ~83% CPU for 720p."
        )
        assert hwdec_for_model("pi3") == "v4l2m2m"

    @pytest.mark.xfail(
        reason="PROFILES['pi3'] is still 1920x1080; capping it at 720p is bundled "
        "with 2.0.0 because it forces a re-transcode of every cached video.",
        strict=True,
    )
    def test_pi3_profile_caps_at_720p(self) -> None:
        """The measured smooth ceiling for VC4 is ~810p, so the profile is 720p.

        Higher resolutions are memory-bandwidth-bound on a shared CPU/GPU, not
        CPU-bound, so no amount of decoder tuning recovers them — measured: 720p
        smooth, 810p smooth (the threshold), 900p+ choppy, and 1080p choppy.

        KNOWN-REMAINING DEFECT: ``PROFILES["pi3"]`` is still 1920x1080, which
        contradicts that measurement.  Changing it invalidates every cached
        1080p video and forces a re-transcode wave, so it is deliberately bundled
        with the 2.0.0 release rather than done in isolation.  This test is
        marked xfail so it acts as a standing reminder instead of noise — flip it
        to a plain assert once the profile is corrected.
        """
        from metixel.backend.processing.video import VideoProcessor

        profile = VideoProcessor.PROFILES["pi3"]
        assert profile["max_width"] <= 1280, (
            "PROFILES['pi3'] must cap at 720p: VC4 is memory-bandwidth-bound above "
            "~810p, and the current 1920x1080 contradicts the measurement."
        )
        assert profile["max_height"] <= 720


class TestMeasuredBaselines:
    """Guard the measured numbers the mapping is justified by.

    These are documentation-shaped assertions: if someone re-measures and the
    hardware decode stops paying off, the mapping should be revisited, and a
    failing test is the prompt to do that.
    """

    def test_hardware_decode_is_substantially_cheaper_pi5(self) -> None:
        row = MEASURED["pi5_hevc"]
        assert row["drm-copy"] < row["no"] / 2, (
            "drm-copy should roughly quarter the CPU for HEVC on a Pi 5"
        )

    def test_hardware_decode_is_substantially_cheaper_pi3(self) -> None:
        row = MEASURED["pi3_h264"]
        assert row["v4l2m2m"] < row["no"] / 2, (
            "v4l2m2m should roughly halve the CPU for H.264 on a Pi 3"
        )

    def test_pi3_auto_is_worse_than_no_hardware_decode(self) -> None:
        """The specific anomaly that makes ``auto`` unusable on a Pi 3."""
        row = MEASURED["pi3_h264"]
        assert row["auto"] > row["no"], (
            "auto exhausted CUDA/Vulkan/drm before reaching v4l2m2m and ended up "
            "MORE expensive than plain software decode"
        )
