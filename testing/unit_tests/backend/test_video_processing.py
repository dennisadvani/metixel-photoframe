# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the Phase 2 processing decomposition.

Covers the new seams extracted from ``VideoProcessor``: ``probe`` (ffprobe
wrappers + RAM/Pi detection), ``ffmpeg_cmds`` (pure command builders),
``frames`` (thumbnail + first/last frame extraction), and the
``needs_optimisation`` threshold gate.  No real ffmpeg/ffprobe is run —
``subprocess`` is mocked throughout.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from metixel.backend.processing.ffmpeg_cmds import (
    compute_thread_limit,
    first_frame_cmd,
    last_frame_cmd,
    probe_cmd,
    select_encoders,
    thumbnail_cmd,
    transcode_cmd,
    validate_cmd,
    wrap_with_throttle,
)
from metixel.backend.processing.frames import (
    cleanup_cached_video,
    extract_thumbnail,
    extract_video_frames,
)
from metixel.backend.processing.probe import (
    available_ram_bytes,
    detect_pi_model,
    probe_video,
    validate_cached_video,
)
from metixel.backend.processing.video import VideoProcessor


class _FakeOpen:
    """Stand-in for ``builtins.open`` keyed by path."""

    def __init__(self, contents: dict[str, str]) -> None:
        self._contents = contents

    def __call__(self, path, *args, **kwargs):
        key = str(path)
        if key in self._contents:
            return io.StringIO(self._contents[key])
        raise FileNotFoundError(key)


def _patch_proc(
    monkeypatch,
    meminfo: str = "MemAvailable: 500000 kB\n",
    model: str | None = "Raspberry Pi 4 Model B Rev 1.4\n",
) -> None:
    """Point /proc reads at in-memory content."""
    contents = {"/proc/meminfo": meminfo}
    if model is not None:
        contents["/proc/device-tree/model"] = model
    monkeypatch.setattr("builtins.open", _FakeOpen(contents))


# ---------------------------------------------------------------------------
# probe.py — RAM / Pi model detection, ffprobe wrappers
# ---------------------------------------------------------------------------


class TestProbe:
    def test_available_ram_bytes_parses_kb(self, monkeypatch):
        _patch_proc(monkeypatch, meminfo="MemAvailable: 512000 kB\n")
        assert available_ram_bytes() == 512000 * 1024

    def test_available_ram_bytes_missing_returns_none(self, monkeypatch):
        _patch_proc(monkeypatch, meminfo="MemTotal: 1000 kB\n")
        assert available_ram_bytes() is None

    def test_available_ram_bytes_no_proc_returns_none(self, monkeypatch):
        monkeypatch.setattr("builtins.open", _FakeOpen({}))
        assert available_ram_bytes() is None

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("Raspberry Pi 5 Model B Rev 1.0\n", "pi5"),
            ("Raspberry Pi 4 Model B Rev 1.4\n", "pi4"),
            ("Raspberry Pi 400 Rev 1.0\n", "pi4"),
            ("Raspberry Pi 3 Model B Plus Rev 1.3\n", "pi3"),
            ("Raspberry Pi 2 Model B Rev 1.1\n", "pi2"),
            ("Raspberry Pi Zero 2 W Rev 1.0\n", "pi3"),
            ("Raspberry Pi Model B Plus Rev 1.2\n", None),
        ],
    )
    def test_detect_pi_model(self, monkeypatch, model, expected):
        _patch_proc(monkeypatch, model=model)
        assert detect_pi_model() == expected

    def test_detect_pi_model_missing_returns_none(self, monkeypatch):
        monkeypatch.setattr("builtins.open", _FakeOpen({}))
        assert detect_pi_model() is None

    def _probe_json(self, **stream_overrides):
        stream = {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "pix_fmt": "yuv420p",
            "r_frame_rate": "30000/1001",
            "bit_rate": "5000000",
            "profile": "High",
            "level": 40,
            "color_primaries": "bt709",
            "color_transfer": "bt709",
            "color_space": "bt709",
        }
        stream.update(stream_overrides)
        return json.dumps({"streams": [stream], "format": {"duration": "10.5"}})

    def test_probe_video_parses_streams(self, monkeypatch):
        fake = mock.MagicMock(return_value=SimpleNamespace(stdout=self._probe_json()))
        monkeypatch.setattr("metixel.backend.processing.probe.subprocess.run", fake)
        info = probe_video(Path("clip.mp4"), timeout=30)
        assert info["width"] == 1920
        assert info["height"] == 1080
        assert info["codec_name"] == "h264"
        assert info["duration"] == 10.5
        assert info["pix_fmt"] == "yuv420p"
        assert info["color_primaries"] == "bt709"
        assert info["color_trc"] == "bt709"
        assert info["colorspace"] == "bt709"
        # bitrate 5,000,000 bps → 5 Mbps
        assert info["bitrate"] == 5

    def test_probe_video_level_normalised_from_int(self, monkeypatch):
        fake = mock.MagicMock(return_value=SimpleNamespace(stdout=self._probe_json(level=40)))
        monkeypatch.setattr("metixel.backend.processing.probe.subprocess.run", fake)
        info = probe_video(Path("clip.mp4"), timeout=30)
        assert info["h264_level"] == 4.0

    def test_probe_video_level_string_parsed(self, monkeypatch):
        fake = mock.MagicMock(return_value=SimpleNamespace(stdout=self._probe_json(level="5.1")))
        monkeypatch.setattr("metixel.backend.processing.probe.subprocess.run", fake)
        info = probe_video(Path("clip.mp4"), timeout=30)
        assert info["h264_level"] == 5.1

    def test_probe_video_fps_parsed(self, monkeypatch):
        fake = mock.MagicMock(
            return_value=SimpleNamespace(stdout=self._probe_json(r_frame_rate="30000/1001"))
        )
        monkeypatch.setattr("metixel.backend.processing.probe.subprocess.run", fake)
        info = probe_video(Path("clip.mp4"), timeout=30)
        assert info["fps"] == 29.97

    def test_probe_video_bitrate_falls_back_to_format(self, monkeypatch):
        payload = json.dumps(
            {
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 100, "height": 100}
                ],
                "format": {"bit_rate": "8000000", "duration": "1.0"},
            }
        )
        fake = mock.MagicMock(return_value=SimpleNamespace(stdout=payload))
        monkeypatch.setattr("metixel.backend.processing.probe.subprocess.run", fake)
        info = probe_video(Path("clip.mp4"), timeout=30)
        assert info["bitrate"] == 8

    def test_probe_video_detects_10bit(self, monkeypatch):
        fake = mock.MagicMock(
            return_value=SimpleNamespace(stdout=self._probe_json(pix_fmt="yuv420p10le"))
        )
        monkeypatch.setattr("metixel.backend.processing.probe.subprocess.run", fake)
        info = probe_video(Path("clip.mp4"), timeout=30)
        assert info["color_depth"] == 10

    def test_probe_video_runs_nice_cmd(self, monkeypatch):
        fake = mock.MagicMock(return_value=SimpleNamespace(stdout=self._probe_json()))
        monkeypatch.setattr("metixel.backend.processing.probe.subprocess.run", fake)
        probe_video(Path("clip.mp4"), timeout=30)
        cmd = fake.call_args[0][0]
        assert "ffprobe" in cmd
        assert fake.call_args[1]["timeout"] == 30

    def test_validate_cached_video_ok(self, monkeypatch):
        fake = mock.MagicMock(return_value=SimpleNamespace(returncode=0, stdout="video"))
        monkeypatch.setattr("metixel.backend.processing.probe.subprocess.run", fake)
        assert validate_cached_video(Path("c.mp4"), timeout=60) is True

    def test_validate_cached_video_bad_returncode(self, monkeypatch):
        fake = mock.MagicMock(return_value=SimpleNamespace(returncode=1, stdout="video"))
        monkeypatch.setattr("metixel.backend.processing.probe.subprocess.run", fake)
        assert validate_cached_video(Path("c.mp4"), timeout=60) is False

    def test_validate_cached_video_no_video_stream(self, monkeypatch):
        fake = mock.MagicMock(return_value=SimpleNamespace(returncode=0, stdout="audio"))
        monkeypatch.setattr("metixel.backend.processing.probe.subprocess.run", fake)
        assert validate_cached_video(Path("c.mp4"), timeout=60) is False

    def test_validate_cached_video_timeout(self, monkeypatch):
        fake = mock.MagicMock(side_effect=TimeoutError("timed out"))
        monkeypatch.setattr("metixel.backend.processing.probe.subprocess.run", fake)
        assert validate_cached_video(Path("c.mp4"), timeout=60) is False


# ---------------------------------------------------------------------------
# ffmpeg_cmds.py — pure command builders
# ---------------------------------------------------------------------------


class TestFfmpegCmds:
    def test_probe_cmd_structure(self):
        cmd = probe_cmd(Path("clip.mp4"))
        assert cmd[0] == "ffprobe"
        assert "-print_format" in cmd and "json" in cmd
        assert cmd[-1] == "clip.mp4"

    def test_validate_cmd_structure(self):
        cmd = validate_cmd(Path("clip.mp4"))
        assert cmd[0] == "ffprobe"
        assert "-show_entries" in cmd
        assert "stream=codec_type" in cmd

    def test_thumbnail_cmd_seeks_2s(self):
        cmd = thumbnail_cmd(Path("in.mp4"), Path("out.jpg"), 1920, 1080)
        assert cmd[0] == "ffmpeg" and "-y" in cmd
        assert cmd[cmd.index("-ss") + 1] == "2"
        assert "-vframes" in cmd
        assert cmd[-1] == "out.jpg"

    def test_first_frame_cmd(self):
        cmd = first_frame_cmd(Path("in.mp4"), Path("out.jpg"), 1920, 1080)
        assert cmd[cmd.index("-ss") + 1] == "0"
        assert "-f" in cmd and "image2" in cmd

    def test_last_frame_cmd_sseof_update(self):
        cmd = last_frame_cmd(Path("in.mp4"), Path("out.jpg"), 1920, 1080)
        assert cmd[cmd.index("-sseof") + 1] == "-1"
        assert "-update" in cmd and "1" in cmd

    def test_scale_filter_even_pad(self):
        from metixel.backend.processing.ffmpeg_cmds import _scale_filter

        f = _scale_filter(1920, 1080)
        assert "scale='min(1920,iw)':'min(1080,ih)'" in f
        assert "pad='ceil(iw/2)*2:ceil(ih/2)*2" in f

    def test_transcode_cmd_libx264_profile(self):
        profile = {
            "codec": "h264",
            "encoder": "libx264",
            "max_width": 1920,
            "max_height": 1080,
            "max_fps": 30,
            "max_bitrate": 7,
            "crf": 28,
            "h264_profile": "high",
            "h264_level": "4.0",
            "color_depth": 8,
            "hdr_support": False,
        }
        info = {"width": 1920, "height": 1080, "fps": 25.0, "bitrate": 5, "color_depth": 8}
        cmd = transcode_cmd(
            Path("in.mp4"),
            Path("out.mp4"),
            "libx264",
            profile,
            info,
            transcode_quality=23,
            thread_limit=2,
            keep_audio=False,
            fallback_max_w=1920,
            fallback_max_h=1080,
        )
        assert cmd[0] == "ffmpeg"
        assert cmd[cmd.index("-c:v") + 1] == "libx264"
        assert cmd[cmd.index("-preset") + 1] == "fast"
        assert cmd[cmd.index("-crf") + 1] == "28"  # profile crf wins over transcode_quality
        assert cmd[cmd.index("-x264-params") + 1] == "threads=2"
        assert cmd[cmd.index("-level") + 1] == "4.0"
        assert cmd[cmd.index("-profile:v") + 1] == "high"
        assert cmd[cmd.index("-r") + 1] == "25.0"
        assert "-an" in cmd  # keep_audio False
        # HDR downgrade (hdr_support False)
        assert cmd[cmd.index("-colorspace") + 1] == "bt709"
        # maxrate = min(src 5, max 7) = 5M
        assert cmd[cmd.index("-maxrate") + 1] == "5M"
        assert cmd[cmd.index("-bufsize") + 1] == "10M"
        assert "-movflags" in cmd and "+faststart" in cmd
        assert cmd[-1] == "out.mp4"

    def test_transcode_cmd_keep_audio(self):
        profile = {"codec": "h264", "encoder": "libx264", "max_width": 1920, "max_height": 1080}
        cmd = transcode_cmd(
            Path("in.mp4"),
            Path("out.mp4"),
            "libx264",
            profile,
            {},
            transcode_quality=23,
            thread_limit=None,
            keep_audio=True,
            fallback_max_w=1280,
            fallback_max_h=720,
        )
        assert "-an" not in cmd
        # no fps info in info={} → no -r flag
        assert "-r" not in cmd

    def test_transcode_cmd_fallback_dims(self):
        profile = {"codec": "h264", "encoder": "libx264"}  # no max_width/max_height keys
        cmd = transcode_cmd(
            Path("in.mp4"),
            Path("out.mp4"),
            "libx264",
            profile,
            {},
            transcode_quality=23,
            thread_limit=None,
            keep_audio=False,
            fallback_max_w=1280,
            fallback_max_h=720,
        )
        vf = cmd[cmd.index("-vf") + 1]
        assert "min(1280,iw)" in vf and "min(720,ih)" in vf

    def test_transcode_cmd_hardware_encoder_bitrate(self):
        profile = {
            "codec": "h264",
            "encoder": "h264_v4l2m2m",
            "max_width": 1920,
            "max_height": 1080,
        }
        cmd = transcode_cmd(
            Path("in.mp4"),
            Path("out.mp4"),
            "h264_v4l2m2m",
            profile,
            {},
            transcode_quality=20,
            thread_limit=None,
            keep_audio=True,
            fallback_max_w=1920,
            fallback_max_h=1080,
        )
        assert cmd[cmd.index("-c:v") + 1] == "h264_v4l2m2m"
        # quality <= 24 → 2M
        assert cmd[cmd.index("-b:v") + 1] == "2M"
        # hardware encoders don't get -preset / -crf / -level / -profile:v
        assert "-preset" not in cmd
        assert "-crf" not in cmd
        assert "-profile:v" not in cmd

    def test_transcode_cmd_libx265_preset_from_config(self, monkeypatch):
        profile = {"codec": "h265", "encoder": "libx265", "max_width": 3840, "max_height": 2160}
        monkeypatch.setattr(
            "metixel.backend.processing.ffmpeg_cmds._libx265_preset", lambda: "superfast"
        )
        cmd = transcode_cmd(
            Path("in.mp4"),
            Path("out.mp4"),
            "libx265",
            profile,
            {},
            transcode_quality=23,
            thread_limit=None,
            keep_audio=False,
            fallback_max_w=3840,
            fallback_max_h=2160,
        )
        assert cmd[cmd.index("-preset") + 1] == "superfast"
        # thread_limit=None → no x265-params
        assert "-x265-params" not in cmd

    def test_wrap_with_throttle_nice_only_when_disabled(self, monkeypatch):
        monkeypatch.setattr("metixel.backend.processing.utils._NICE_BINARY", "/usr/bin/nice")
        cmd = wrap_with_throttle(
            ["ffmpeg", "-i", "in"], cpu_throttle_enabled=False, cpu_throttle_pct=100
        )
        assert cmd[:3] == ["nice", "-n", "19"]
        assert "cpulimit" not in cmd

    def test_wrap_with_throttle_cpulimit_when_enabled(self, monkeypatch):
        monkeypatch.setattr("metixel.backend.processing.utils._NICE_BINARY", "/usr/bin/nice")
        monkeypatch.setattr(
            "metixel.backend.processing.ffmpeg_cmds.shutil.which",
            lambda name: "/usr/bin/cpulimit",
        )
        cmd = wrap_with_throttle(
            ["ffmpeg", "-i", "in"], cpu_throttle_enabled=True, cpu_throttle_pct=200
        )
        assert cmd[0] == "cpulimit"
        assert cmd[cmd.index("-l") + 1] == "200"
        assert "-f" in cmd  # foreground
        assert cmd[cmd.index("--") + 1 : cmd.index("--") + 4] == ["nice", "-n", "19"]
        assert cmd[-3:] == ["ffmpeg", "-i", "in"]

    def test_compute_thread_limit_disabled(self):
        assert compute_thread_limit(False, 200) is None

    def test_compute_thread_limit_high_pct_auto(self, monkeypatch):
        monkeypatch.setattr("metixel.backend.processing.ffmpeg_cmds.os.cpu_count", lambda: 4)
        assert compute_thread_limit(True, 500) is None

    def test_compute_thread_limit_mapping(self, monkeypatch):
        monkeypatch.setattr("metixel.backend.processing.ffmpeg_cmds.os.cpu_count", lambda: 4)
        assert compute_thread_limit(True, 50) == 1
        assert compute_thread_limit(True, 150) == 2
        assert compute_thread_limit(True, 350) == 4
        assert compute_thread_limit(True, 400) == 4

    def test_select_encoders_software_forced(self):
        assert select_encoders(force_software_encoder=True, timeout=30) == ["libx264"]

    def test_select_encoders_hardware_detection(self, monkeypatch):
        fake = mock.MagicMock(
            return_value=SimpleNamespace(stdout="h264_v4l2m2m\nh264_mmal\nh264_vaapi")
        )
        monkeypatch.setattr("metixel.backend.processing.ffmpeg_cmds.subprocess.run", fake)
        encoders = select_encoders(force_software_encoder=False, timeout=30)
        assert encoders[:3] == ["h264_v4l2m2m", "h264_mmal", "h264_vaapi"]
        assert encoders[-1] == "libx264"

    def test_libx265_preset_ultrafast_on_low_ram(self, monkeypatch):
        from metixel.backend.processing.ffmpeg_cmds import _libx265_preset

        _patch_proc(monkeypatch, meminfo="MemTotal: 2097152 kB\n")  # 2 GB
        assert _libx265_preset() == "ultrafast"

    def test_libx265_preset_superfast_on_high_ram(self, monkeypatch):
        from metixel.backend.processing.ffmpeg_cmds import _libx265_preset

        _patch_proc(monkeypatch, meminfo="MemTotal: 8388608 kB\n")  # 8 GB
        assert _libx265_preset() == "superfast"


# ---------------------------------------------------------------------------
# frames.py — thumbnail + first/last frame extraction, cache cleanup
# ---------------------------------------------------------------------------


class TestFrames:
    def test_extract_thumbnail_runs_nice_cmd(self, monkeypatch):
        fake = mock.MagicMock()
        monkeypatch.setattr("metixel.backend.processing.frames.subprocess.run", fake)
        extract_thumbnail(Path("in.mp4"), Path("out.jpg"), 1920, 1080, timeout=300)
        cmd = fake.call_args[0][0]
        assert "ffmpeg" in cmd
        assert fake.call_args[1]["check"] is True
        assert fake.call_args[1]["timeout"] == 300

    def test_extract_video_frames_success(self, monkeypatch, tmp_path):
        fake = mock.MagicMock()
        monkeypatch.setattr("metixel.backend.processing.frames.subprocess.run", fake)
        first, last = extract_video_frames(
            Path("in.mp4"), "abc123", tmp_path, 1920, 1080, timeout_fn=lambda key, default: default
        )
        assert first == tmp_path / "abc123.1.frame.jpg"
        assert last == tmp_path / "abc123.2.frame.jpg"
        assert fake.call_count == 2
        # timeout_fn invoked with the per-frame keys
        args = [c[0][0] for c in fake.call_args_list]
        assert any("-sseof" in a for a in args)  # last frame cmd

    def test_extract_video_frames_skips_existing(self, monkeypatch, tmp_path):
        (tmp_path / "abc123.1.frame.jpg").write_bytes(b"jpeg")
        (tmp_path / "abc123.2.frame.jpg").write_bytes(b"jpeg")
        fake = mock.MagicMock()
        monkeypatch.setattr("metixel.backend.processing.frames.subprocess.run", fake)
        first, last = extract_video_frames(
            Path("in.mp4"), "abc123", tmp_path, 1920, 1080, timeout_fn=lambda key, default: default
        )
        assert first is not None and last is not None
        fake.assert_not_called()

    def test_extract_video_frames_failure_returns_none(self, monkeypatch, tmp_path):
        import subprocess

        fake = mock.MagicMock(side_effect=subprocess.CalledProcessError(1, "ffmpeg"))
        monkeypatch.setattr("metixel.backend.processing.frames.subprocess.run", fake)
        first, last = extract_video_frames(
            Path("in.mp4"), "abc123", tmp_path, 1920, 1080, timeout_fn=lambda key, default: default
        )
        assert first is None
        assert last is None
        assert not (tmp_path / "abc123.1.frame.jpg").exists()

    def test_cleanup_cached_video_keeps_source_derived_frames(self, tmp_path):
        """Only the .mp4 goes.  Frames (like the thumbnail) are extracted
        from the SOURCE, and nothing re-extracts them after cleanup — deleting
        them left the MediaItem pointing at missing files."""
        cached = tmp_path / "video.mp4"
        frame1 = tmp_path / "abc123.1.frame.jpg"
        frame2 = tmp_path / "abc123.2.frame.jpg"
        thumb = tmp_path / "thumb.jpg"
        for p in (cached, frame1, frame2, thumb):
            p.write_bytes(b"x")
        cleanup_cached_video(cached, "abc123")
        assert not cached.exists()
        assert frame1.exists()
        assert frame2.exists()
        assert thumb.exists()  # thumbnail is independent — kept

    def test_cleanup_cached_video_missing_file_is_noop(self, tmp_path):
        cleanup_cached_video(tmp_path / "gone.mp4", "abc123")  # must not raise


# ---------------------------------------------------------------------------
# video.py — needs_optimisation threshold gate + _hash_file
# ---------------------------------------------------------------------------


class TestNeedsOptimisation:
    H264_OK = {
        "width": 1920,
        "height": 1080,
        "codec_name": "h264",
        "fps": 25.0,
        "bitrate": 5,
        "color_depth": 8,
        "h264_level": "4.0",
        "color_trc": "bt709",
    }
    PROFILE = {
        "codec": "h264",
        "max_width": 1920,
        "max_height": 1080,
        "max_fps": 30,
        "max_bitrate": 7,
        "color_depth": 8,
        "hdr_support": False,
        "h264_level": "4.0",
    }

    def test_no_dimensions_returns_true(self):
        assert (
            VideoProcessor.needs_optimisation({"width": 0, "height": 1080, "codec_name": "h264"})
            is True
        )

    def test_no_profile_h264_ok(self):
        assert (
            VideoProcessor.needs_optimisation({"width": 100, "height": 100, "codec_name": "h264"})
            is False
        )

    def test_no_profile_hevc_needs_transcode(self):
        assert (
            VideoProcessor.needs_optimisation({"width": 100, "height": 100, "codec_name": "hevc"})
            is True
        )

    def test_within_all_limits_false(self):
        assert VideoProcessor.needs_optimisation(self.H264_OK, self.PROFILE) is False

    def test_non_h264_codec_true(self):
        info = dict(self.H264_OK, codec_name="hevc")
        assert VideoProcessor.needs_optimisation(info, self.PROFILE) is True

    def test_resolution_above_max_true(self):
        info = dict(self.H264_OK, width=2560)
        assert VideoProcessor.needs_optimisation(info, self.PROFILE) is True
        info = dict(self.H264_OK, height=1200)
        assert VideoProcessor.needs_optimisation(info, self.PROFILE) is True

    def test_fps_above_max_true(self):
        info = dict(self.H264_OK, fps=60.0)
        assert VideoProcessor.needs_optimisation(info, self.PROFILE) is True

    def test_bitrate_above_max_true(self):
        info = dict(self.H264_OK, bitrate=8)  # > 7 * 1.1
        assert VideoProcessor.needs_optimisation(info, self.PROFILE) is True

    def test_color_depth_above_true(self):
        info = dict(self.H264_OK, color_depth=10)
        assert VideoProcessor.needs_optimisation(info, self.PROFILE) is True

    def test_hdr_source_true(self):
        info = dict(self.H264_OK, color_trc="smpte2084")
        assert VideoProcessor.needs_optimisation(info, self.PROFILE) is True

    def test_h264_level_above_true(self):
        info = dict(self.H264_OK, h264_level="5.1")
        assert VideoProcessor.needs_optimisation(info, self.PROFILE) is True

    def test_h264_level_at_or_below_false(self):
        info = dict(self.H264_OK, h264_level="4.0")
        assert VideoProcessor.needs_optimisation(info, self.PROFILE) is False

    H265_PROFILE = {
        "codec": "h265",
        "encoder": "libx265",
        "max_width": 3840,
        "max_height": 2160,
        "max_fps": 60,
        "max_bitrate": 40,
        "color_depth": 10,
        "hdr_support": True,
        "h264_level": "5.1",
    }

    def test_h264_source_on_h265_profile_still_needs_transcode(self):
        """The SOURCE decision is unchanged: H.264 in → transcode to HEVC."""
        assert VideoProcessor.needs_optimisation(self.H264_OK, self.H265_PROFILE) is True

    def test_h264_cache_accepted_on_h265_profile_with_fallback(self):
        """A libx264-fallback cache (libx265 failed) is a valid output for an
        H.265 profile — it must not be deleted and re-encoded every boot."""
        assert (
            VideoProcessor.needs_optimisation(
                self.H264_OK, self.H265_PROFILE, accept_fallback_codecs=True
            )
            is False
        )

    def test_fallback_acceptance_still_enforces_limits(self):
        too_wide = dict(self.H264_OK, width=7680)
        assert (
            VideoProcessor.needs_optimisation(
                too_wide, self.H265_PROFILE, accept_fallback_codecs=True
            )
            is True
        )
        too_high_level = dict(self.H264_OK, h264_level="6.2")
        assert (
            VideoProcessor.needs_optimisation(
                too_high_level, self.H265_PROFILE, accept_fallback_codecs=True
            )
            is True
        )

    def test_fallback_acceptance_rejects_unrelated_codec(self):
        vp9 = dict(self.H264_OK, codec_name="vp9")
        assert (
            VideoProcessor.needs_optimisation(vp9, self.H265_PROFILE, accept_fallback_codecs=True)
            is True
        )

    def test_fallback_encoders_for_profile(self):
        assert VideoProcessor.fallback_encoders_for_profile(self.H265_PROFILE) == [
            "libx265",
            "libx264",
        ]
        assert VideoProcessor.fallback_encoders_for_profile({"encoder": "libx264"}) == ["libx264"]
        assert VideoProcessor.fallback_encoders_for_profile({}) == ["libx264"]
        assert VideoProcessor.codecs_for_encoder("libx265") == VideoProcessor.HEVC_CODECS
        assert VideoProcessor.codecs_for_encoder("h264_v4l2m2m") == VideoProcessor.H264_CODECS

    def test_hash_file_stable(self, tmp_path):
        f = tmp_path / "video.bin"
        f.write_bytes(b"\x00" * 4096)
        h1 = VideoProcessor._hash_file(f)
        h2 = VideoProcessor._hash_file(f)
        assert h1 == h2
        assert len(h1) == 16
        assert all(c in "0123456789abcdef" for c in h1)


# ---------------------------------------------------------------------------
# video.py — process() cache-miss logging (no real ffmpeg is run)
# ---------------------------------------------------------------------------


class TestProcessCacheMissLogging:
    """Exercise the explicit cache-miss log line in ``VideoProcessor.process()``.

    The processor's seams are mocked so no ffmpeg/ffprobe runs.  The source
    is HEVC (so ``needs_optimisation`` is True and the transcode path is
    reached), and the cached file either exists or not to toggle the branch.
    """

    FILE_HASH = "abcdef1234567890"

    HEVC_SOURCE = {
        "width": 1920,
        "height": 1080,
        "codec_name": "hevc",
        "fps": 25.0,
        "bitrate": 5,
        "color_depth": 8,
        "duration": 10.0,
    }
    # A cached file that is within the H.264 profile limits.
    H264_CACHED = {
        "width": 1920,
        "height": 1080,
        "codec_name": "h264",
        "fps": 25.0,
        "bitrate": 5,
        "color_depth": 8,
        "h264_level": "4.0",
        "color_trc": "bt709",
    }

    def _make_processor(self, tmp_path) -> VideoProcessor:
        return VideoProcessor(
            cache_dir=tmp_path / "cache",
            screen_width=1920,
            screen_height=1080,
            video_config={
                "transcoding_enabled": True,
                "transcoding_profile": "pi3",
            },
        )

    def _mock_seams(self, proc: VideoProcessor, tmp_path, cached: Path | None) -> None:
        """Wire up mocks so ``process()`` can run without external tools."""
        proc._hash_file = mock.Mock(return_value=self.FILE_HASH)  # type: ignore[method-assign]

        def fake_probe(path):
            # The cached file probes as already-optimal H.264; the source is HEVC.
            if cached is not None and str(path) == str(cached):
                return dict(self.H264_CACHED)
            return dict(self.HEVC_SOURCE)

        proc._probe = mock.Mock(side_effect=fake_probe)  # type: ignore[method-assign]
        proc._extract_thumbnail = mock.Mock(return_value=None)  # type: ignore[method-assign]
        proc._extract_video_frames = mock.Mock(  # type: ignore[method-assign]
            return_value=(tmp_path / "f1.jpg", tmp_path / "f2.jpg")
        )
        proc._resolve_profile = mock.Mock(  # type: ignore[method-assign]
            return_value={
                "codec": "h264",
                "max_width": 1920,
                "max_height": 1080,
                "max_fps": 30,
                "max_bitrate": 7,
                "color_depth": 8,
                "hdr_support": False,
                "h264_level": "4.0",
            }
        )
        proc._validate_cached_video = mock.Mock(return_value=True)  # type: ignore[method-assign]
        proc._transcode = mock.Mock()  # type: ignore[method-assign]
        proc._build_item = mock.Mock(return_value="built-item")  # type: ignore[method-assign]

    def test_cache_miss_logs_no_cached_video(self, tmp_path, caplog):
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"x" * 1024)
        proc = self._make_processor(tmp_path)
        self._mock_seams(proc, tmp_path, cached=None)

        with caplog.at_level(logging.INFO, logger="metixel.backend.processing.video"):
            result = proc.process(source, source="local")

        assert result == "built-item"
        assert "No cached video found for clip.mp4 — transcoding" in caplog.text
        proc._transcode.assert_called_once()

    def test_cache_hit_does_not_log_miss(self, tmp_path, caplog):
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"x" * 1024)
        proc = self._make_processor(tmp_path)
        cached = proc._video_cache / f"{self.FILE_HASH}.mp4"
        cached.write_bytes(b"x" * 2048)
        self._mock_seams(proc, tmp_path, cached=cached)

        with caplog.at_level(logging.INFO, logger="metixel.backend.processing.video"):
            result = proc.process(source, source="local")

        assert result == "built-item"
        assert "No cached video found" not in caplog.text
        proc._transcode.assert_not_called()
        proc._build_item.assert_called_once()


# ---------------------------------------------------------------------------
# video.py — scan()/transcode() split (full-profile classification)
# ---------------------------------------------------------------------------


class TestVideoScanTranscode:
    """The two-phase split: ``scan()`` probes/thumbs/frames and decides, using
    the full profile check, whether the video needs transcoding; ``transcode()``
    turns the scan into a playable item.  No real ffmpeg/ffprobe runs.
    """

    H264_SOURCE = {
        "width": 1920,
        "height": 1080,
        "codec_name": "h264",
        "fps": 25.0,
        "bitrate": 5,
        "color_depth": 8,
        "duration": 10.0,
    }

    def _make_proc(self, tmp_path, profile):
        from metixel.backend.processing.video import VideoProcessor

        p = VideoProcessor(
            cache_dir=tmp_path / "cache",
            screen_width=1920,
            screen_height=1080,
            video_config={
                "transcoding_enabled": True,
                "transcoding_profile": "custom",
            },
        )
        p._hash_file = mock.Mock(return_value="feedface12345678")  # type: ignore[method-assign]
        p._probe = mock.Mock(return_value=dict(self.H264_SOURCE))  # type: ignore[method-assign]
        p._extract_thumbnail = mock.Mock()  # type: ignore[method-assign]
        p._extract_video_frames = mock.Mock(return_value=(Path("/tmp/f1.jpg"), Path("/tmp/f2.jpg")))  # type: ignore[method-assign]
        p._resolve_profile = mock.Mock(return_value=profile)  # type: ignore[method-assign]
        return p

    def test_h264_source_on_h265_profile_needs_transcode(self, tmp_path) -> None:
        """The bug case: H.264 source + H.265 target profile → transcode needed."""
        h265_profile = {
            "codec": "h265",
            "max_width": 3840,
            "max_height": 2160,
            "max_fps": 60,
            "max_bitrate": 80,
            "color_depth": 10,
            "hdr_support": True,
        }
        p = self._make_proc(tmp_path, h265_profile)
        scan = p.scan(tmp_path / "clip.mp4")
        assert scan is not None
        assert scan.needs_transcode is True
        assert scan.has_frames is True
        assert scan.errors == []

    def test_h264_source_within_h264_profile_no_transcode(self, tmp_path) -> None:
        h264_profile = {
            "codec": "h264",
            "max_width": 1920,
            "max_height": 1080,
            "max_fps": 30,
            "max_bitrate": 7,
            "color_depth": 8,
            "hdr_support": False,
            "h264_level": "4.0",
        }
        p = self._make_proc(tmp_path, h264_profile)
        scan = p.scan(tmp_path / "clip.mp4")
        assert scan is not None
        assert scan.needs_transcode is False

    def test_scan_records_missing_frames_error(self, tmp_path) -> None:
        p = self._make_proc(tmp_path, None)
        p._extract_video_frames = mock.Mock(return_value=(None, None))  # type: ignore[method-assign]
        scan = p.scan(tmp_path / "clip.mp4")
        assert scan is not None
        assert scan.has_frames is False
        assert scan.errors, "expected a frame-extraction error to be recorded"

    def test_scan_returns_none_when_unreadable(self, tmp_path) -> None:

        p = self._make_proc(tmp_path, None)
        p._probe = mock.Mock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        assert p.scan(tmp_path / "clip.mp4") is None

    def test_transcode_no_transcode_returns_not_transcoded(self, tmp_path) -> None:
        from metixel.shared.models import TranscodeStatus

        h264_profile = {
            "codec": "h264",
            "max_width": 1920,
            "max_height": 1080,
            "max_fps": 30,
            "max_bitrate": 7,
            "color_depth": 8,
            "hdr_support": False,
            "h264_level": "4.0",
        }
        p = self._make_proc(tmp_path, h264_profile)
        scan = p.scan(tmp_path / "clip.mp4")
        assert scan is not None and scan.needs_transcode is False
        result = p.transcode(scan)
        assert result is not None
        assert result.transcode_status == TranscodeStatus.NOT_TRANSCODED
        assert result.cached_path == result.original_path

    def test_requires_encode_missing_cache_true(self, tmp_path) -> None:
        """A video that needs transcode with no cache file requires an encode."""
        h265_profile = {
            "codec": "h265",
            "max_width": 3840,
            "max_height": 2160,
            "max_fps": 60,
            "max_bitrate": 80,
            "color_depth": 10,
            "hdr_support": True,
        }
        p = self._make_proc(tmp_path, h265_profile)
        scan = p.scan(tmp_path / "clip.mp4")
        assert scan is not None and scan.needs_transcode is True
        assert p.requires_encode(scan) is True

    def test_requires_encode_no_transcode_false(self, tmp_path) -> None:
        h264_profile = {
            "codec": "h264",
            "max_width": 1920,
            "max_height": 1080,
            "max_fps": 30,
            "max_bitrate": 7,
            "color_depth": 8,
            "hdr_support": False,
            "h264_level": "4.0",
        }
        p = self._make_proc(tmp_path, h264_profile)
        scan = p.scan(tmp_path / "clip.mp4")
        assert scan is not None and scan.needs_transcode is False
        assert p.requires_encode(scan) is False

    def test_requires_encode_valid_cache_false(self, tmp_path) -> None:
        """A valid in-limits cache means no encode is needed (cache reuse)."""
        h265_profile = {
            "codec": "h265",
            "max_width": 3840,
            "max_height": 2160,
            "max_fps": 60,
            "max_bitrate": 80,
            "color_depth": 10,
            "hdr_support": True,
        }
        p = self._make_proc(tmp_path, h265_profile)
        scan = p.scan(tmp_path / "clip.mp4")
        assert scan is not None and scan.needs_transcode is True

        cached = p._video_cache / f"{scan.file_hash}.mp4"
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(b"x" * 2048)

        def fake_probe(path):
            if str(path) == str(cached):
                # cached output is in-limits H.265
                return {
                    "width": 1280,
                    "height": 720,
                    "codec_name": "hevc",
                    "fps": 25.0,
                    "bitrate": 3,
                    "color_depth": 8,
                    "h264_level": "",
                    "color_trc": "bt709",
                }
            return dict(self.H264_SOURCE)

        p._validate_cached_video = mock.Mock(return_value=True)  # type: ignore[method-assign]
        p._probe = mock.Mock(side_effect=fake_probe)  # type: ignore[method-assign]
        assert p.requires_encode(scan) is False


# ---------------------------------------------------------------------------
# video.py — libx264-fallback cache on an H.265 profile is reused, not re-encoded
# ---------------------------------------------------------------------------


class TestFallbackCacheReuse:
    """Pi 4/5 profiles encode with libx265 and fall back to libx264.  The
    resulting H.264 cache must be accepted on the next scan instead of being
    deleted and re-encoded on every boot."""

    H264_SOURCE = {
        "width": 3840,
        "height": 2160,
        "codec_name": "h264",
        "fps": 30.0,
        "bitrate": 50,
        "color_depth": 8,
        "duration": 10.0,
    }
    H264_CACHE = {
        "width": 1920,
        "height": 1080,
        "codec_name": "h264",
        "fps": 30.0,
        "bitrate": 8,
        "color_depth": 8,
        "h264_level": "4.2",
        "color_trc": "bt709",
    }
    H265_PROFILE = {
        "codec": "h265",
        "encoder": "libx265",
        "max_width": 3840,
        "max_height": 2160,
        "max_fps": 60,
        "max_bitrate": 40,
        "color_depth": 10,
        "hdr_support": True,
        "h264_level": "5.1",
    }

    def _proc(self, tmp_path):
        p = VideoProcessor(cache_dir=tmp_path / "cache", video_config={"transcoding_enabled": True})
        p._hash_file = mock.Mock(return_value="cafe0000cafe0000")  # type: ignore[method-assign]
        p._extract_thumbnail = mock.Mock()  # type: ignore[method-assign]
        p._extract_video_frames = mock.Mock(  # type: ignore[method-assign]
            return_value=(tmp_path / "f1.jpg", tmp_path / "f2.jpg")
        )
        p._resolve_profile = mock.Mock(return_value=dict(self.H265_PROFILE))  # type: ignore[method-assign]
        p._validate_cached_video = mock.Mock(return_value=True)  # type: ignore[method-assign]
        p._transcode = mock.Mock()  # type: ignore[method-assign]
        cached = p._video_cache / "cafe0000cafe0000.mp4"
        cached.write_bytes(b"x" * 4096)

        def fake_probe(path):
            return dict(self.H264_CACHE) if str(path) == str(cached) else dict(self.H264_SOURCE)

        p._probe = mock.Mock(side_effect=fake_probe)  # type: ignore[method-assign]
        return p, cached

    def test_h264_fallback_cache_reused(self, tmp_path):
        from metixel.shared.models import TranscodeStatus

        p, cached = self._proc(tmp_path)
        scan = p.scan(tmp_path / "clip.mp4")
        assert scan is not None and scan.needs_transcode is True
        assert p.requires_encode(scan) is False
        item = p.transcode(scan)
        assert item is not None
        assert item.cached_path == cached
        assert item.transcode_status == TranscodeStatus.TRANSCODED
        assert cached.exists()
        p._transcode.assert_not_called()


# ---------------------------------------------------------------------------
# utils.run_in_session — process-group kill on timeout
# ---------------------------------------------------------------------------


class _FakePopen:
    """Popen stand-in: first communicate() times out, the second reaps."""

    instances: list = []

    def __init__(self, args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = None
        self.calls = 0
        self.killed = False
        _FakePopen.instances.append(self)

    def communicate(self, timeout=None):
        import subprocess

        self.calls += 1
        if self.calls == 1 and self.kwargs.get("_timeout_first", True):
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.returncode = -9
        return (b"", b"")

    def kill(self) -> None:
        self.killed = True


class TestRunInSession:
    def _patch(self, monkeypatch):
        import metixel.backend.processing.utils as utils

        _FakePopen.instances.clear()
        signals: list[int] = []
        monkeypatch.setattr(utils.subprocess, "Popen", _FakePopen)
        monkeypatch.setattr(utils.os, "name", "posix")
        monkeypatch.setattr(utils.os, "getpgid", lambda pid: 9000 + pid)
        monkeypatch.setattr(utils.os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
        return utils, signals

    def test_timeout_kills_whole_group_cont_then_kill(self, monkeypatch):
        import signal
        import subprocess

        utils, signals = self._patch(monkeypatch)
        with pytest.raises(subprocess.TimeoutExpired):
            utils.run_in_session(["cpulimit", "--", "ffmpeg"], timeout=1)

        proc = _FakePopen.instances[0]
        assert proc.kwargs["start_new_session"] is True
        assert signals == [(9000 + 4242, signal.SIGCONT), (9000 + 4242, signal.SIGKILL)]
        assert proc.calls == 2, "the killed group must be reaped"

    def test_success_returns_completed_process(self, monkeypatch):
        utils, signals = self._patch(monkeypatch)

        class _OkPopen(_FakePopen):
            def communicate(self, timeout=None):
                self.returncode = 0
                return ("out", "")

        monkeypatch.setattr(utils.subprocess, "Popen", _OkPopen)
        result = utils.run_in_session(["ffmpeg"], timeout=5, stdout=None)
        assert result.returncode == 0 and result.stdout == "out"
        assert signals == []

    def test_check_raises_called_process_error(self, monkeypatch):
        import subprocess

        utils, _ = self._patch(monkeypatch)

        class _FailPopen(_FakePopen):
            def communicate(self, timeout=None):
                self.returncode = 1
                return (b"", b"")

        monkeypatch.setattr(utils.subprocess, "Popen", _FailPopen)
        with pytest.raises(subprocess.CalledProcessError):
            utils.run_in_session(["ffmpeg"], timeout=5, check=True)


class TestTranscodeUsesSessionRunner:
    """``_transcode`` keeps its encoder fallback while going through
    ``run_in_session`` and honours ``transcode_use_software_encoder``."""

    PROFILE = {
        "codec": "h265",
        "encoder": "libx265",
        "max_width": 3840,
        "max_height": 2160,
        "max_fps": 60,
        "max_bitrate": 40,
        "color_depth": 10,
        "hdr_support": True,
    }

    def _proc(self, tmp_path, monkeypatch, sw=True):
        p = VideoProcessor(
            cache_dir=tmp_path / "cache",
            video_config={"transcoding_enabled": True, "transcode_use_software_encoder": sw},
        )
        p._resolve_profile = mock.Mock(return_value=dict(self.PROFILE))  # type: ignore[method-assign]
        monkeypatch.setattr("metixel.backend.processing.video.available_ram_bytes", lambda: None)
        monkeypatch.setattr(
            "metixel.backend.processing.video.wrap_with_throttle", lambda cmd, *a: list(cmd)
        )
        return p

    def test_timeout_on_first_encoder_falls_back_to_libx264(self, tmp_path, monkeypatch):
        import subprocess

        p = self._proc(tmp_path, monkeypatch)
        dest = p._video_cache / "out.mp4"
        calls: list[list[str]] = []

        def fake_run(cmd, *, timeout, check=False, **kw):
            calls.append(list(cmd))
            encoder = cmd[cmd.index("-c:v") + 1]
            if encoder == "libx265":
                dest.write_bytes(b"partial")
                raise subprocess.TimeoutExpired(cmd, timeout)
            dest.write_bytes(b"ok")
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr("metixel.backend.processing.video.run_in_session", fake_run)
        p._transcode(tmp_path / "in.mp4", dest, {"width": 1920, "height": 1080})

        encoders = [c[c.index("-c:v") + 1] for c in calls]
        assert encoders == ["libx265", "libx264"]
        assert dest.read_bytes() == b"ok"

    def test_all_encoders_fail_raises_runtime_error(self, tmp_path, monkeypatch):
        import subprocess

        p = self._proc(tmp_path, monkeypatch)
        dest = p._video_cache / "out.mp4"

        def fake_run(cmd, *, timeout, check=False, **kw):
            raise subprocess.CalledProcessError(1, cmd)

        monkeypatch.setattr("metixel.backend.processing.video.run_in_session", fake_run)
        with pytest.raises(RuntimeError, match="All encoders failed"):
            p._transcode(tmp_path / "in.mp4", dest, {})

    def test_hardware_encoders_used_when_software_not_forced(self, tmp_path, monkeypatch):
        p = self._proc(tmp_path, monkeypatch, sw=False)
        monkeypatch.setattr(
            "metixel.backend.processing.video.select_encoders",
            lambda force, timeout: ["libx264"] if force else ["h264_v4l2m2m", "libx264"],
        )
        assert p._encoders_for_profile(self.PROFILE) == ["libx265", "h264_v4l2m2m", "libx264"]
        p_sw = self._proc(tmp_path, monkeypatch, sw=True)
        assert p_sw._encoders_for_profile(self.PROFILE) == ["libx265", "libx264"]
        assert p_sw._encoders_for_profile({"encoder": "libx264"}) == ["libx264"]


# ---------------------------------------------------------------------------
# probe.py — H.264 level 1b and 10-bit pixel-format detection
# ---------------------------------------------------------------------------


class TestProbeNormalisation:
    def _run(self, monkeypatch, **overrides):
        payload = TestProbe()._probe_json(**overrides)
        fake = mock.MagicMock(return_value=SimpleNamespace(stdout=payload))
        monkeypatch.setattr("metixel.backend.processing.probe.subprocess.run", fake)
        return probe_video(Path("clip.mp4"), timeout=30)

    def test_level_9_is_1b(self, monkeypatch):
        assert self._run(monkeypatch, level=9)["h264_level"] == 1.0

    def test_negative_level_is_unknown(self, monkeypatch):
        assert self._run(monkeypatch, level=-99)["h264_level"] == ""

    def test_level_string_1b(self, monkeypatch):
        assert self._run(monkeypatch, level="1b")["h264_level"] == 1.0

    @pytest.mark.parametrize(
        "pix_fmt,depth",
        [
            ("yuv410p", 8),
            ("yuv411p", 8),
            ("yuv420p", 8),
            ("yuv420p10le", 10),
            ("yuv444p10be", 10),
            ("p010le", 10),
            ("yuv420p12le", 12),
            ("p012le", 12),
        ],
    )
    def test_color_depth_from_pix_fmt(self, monkeypatch, pix_fmt, depth):
        assert self._run(monkeypatch, pix_fmt=pix_fmt)["color_depth"] == depth


# ---------------------------------------------------------------------------
# thumbnail.py / worker.py — palette+alpha images and video thumbnail scaling
# ---------------------------------------------------------------------------


class TestThumbnailFixes:
    def test_video_thumbnail_is_downscaled(self, tmp_path, monkeypatch):
        from metixel.backend.processing import thumbnail as th

        src = tmp_path / "clip.mp4"
        src.write_bytes(b"\x00" * 4096)
        fake = mock.MagicMock()
        monkeypatch.setattr("metixel.backend.processing.thumbnail.subprocess.run", fake)

        th.generate_video_thumbnail(src, tmp_path / "cache")

        cmd = fake.call_args[0][0]
        assert "-vf" in cmd
        vf = cmd[cmd.index("-vf") + 1]
        assert f"min({th.THUMBNAIL_SIZE},iw)" in vf and "force_original_aspect_ratio=decrease" in vf
        assert cmd.index("-vf") < cmd.index("-vframes")

    @pytest.mark.parametrize("mode", ["PA", "RGBA", "LA"])
    def test_image_thumbnail_handles_alpha_modes(self, tmp_path, monkeypatch, mode):
        """``bg.paste(img, img)`` raised ValueError for "PA" images.

        PIL cannot write PA to a file, so the source image is served from
        memory for the source path only (everything else uses the real open).
        """
        from PIL import Image

        from metixel.backend.processing import thumbnail as th

        src = tmp_path / f"alpha_{mode}.png"
        src.write_bytes(b"\x00" * 2048)  # content_hash needs a real file
        img = Image.new("RGBA", (64, 48), (255, 0, 0, 0))
        if mode == "PA":
            img = img.convert("P").convert("PA")
        elif mode == "LA":
            img = img.convert("LA")
        assert img.mode == mode
        real_open = Image.open

        def fake_open(fp, *a, **kw):
            return img if Path(str(fp)) == src else real_open(fp, *a, **kw)

        monkeypatch.setattr(th.Image, "open", fake_open)
        thumb = th.generate_image_thumbnail(src, tmp_path / "cache")
        assert thumb is not None and thumb.is_file()
        with real_open(thumb) as t:
            assert t.mode == "RGB"
            # transparent → black (JPEG rounding allows ±4), not white
            assert all(c <= 4 for c in t.getpixel((0, 0)))

    def test_palette_with_transparency_composited_on_black(self, tmp_path):
        from PIL import Image

        from metixel.backend.processing import thumbnail as th

        src = tmp_path / "pal.png"
        img = Image.new("P", (16, 16), 0)
        img.putpalette([255, 255, 255] * 256)
        img.info["transparency"] = 0
        img.save(src, "PNG", transparency=0)
        thumb = th.generate_image_thumbnail(src, tmp_path / "cache")
        assert thumb is not None
        with Image.open(thumb) as t:
            assert all(c <= 4 for c in t.getpixel((0, 0)))

    def test_worker_handles_pa_image(self, tmp_path, monkeypatch):
        import argparse

        from PIL import Image

        from metixel.backend.processing import worker

        src = tmp_path / "pa.png"
        src.write_bytes(b"\x00" * 2048)
        img = Image.new("RGBA", (64, 48), (255, 0, 0, 0)).convert("P").convert("PA")
        real_open = Image.open

        def fake_open(fp, *a, **kw):
            return img if Path(str(fp)) == src else real_open(fp, *a, **kw)

        monkeypatch.setattr(Image, "open", fake_open)
        args = argparse.Namespace(
            source=str(src),
            cache=str(tmp_path / "c" / "out.jpg"),
            thumb=str(tmp_path / "t" / "out.jpg"),
            screen=(1920, 1080),
        )
        result = worker._process(args)
        assert result["status"] == "ok"
        with real_open(args.cache) as cached:
            assert cached.mode == "RGB"
            assert all(c <= 4 for c in cached.getpixel((0, 0)))
