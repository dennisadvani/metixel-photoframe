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

    def test_cleanup_cached_video(self, tmp_path):
        cached = tmp_path / "video.mp4"
        frame1 = tmp_path / "abc123.1.frame.jpg"
        frame2 = tmp_path / "abc123.2.frame.jpg"
        thumb = tmp_path / "thumb.jpg"
        for p in (cached, frame1, frame2, thumb):
            p.write_bytes(b"x")
        cleanup_cached_video(cached, "abc123")
        assert not cached.exists()
        assert not frame1.exists()
        assert not frame2.exists()
        assert thumb.exists()  # thumbnail is independent — kept


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

    def test_hash_file_stable(self, tmp_path):
        f = tmp_path / "video.bin"
        f.write_bytes(b"\x00" * 4096)
        h1 = VideoProcessor._hash_file(f)
        h2 = VideoProcessor._hash_file(f)
        assert h1 == h2
        assert len(h1) == 16
        assert all(c in "0123456789abcdef" for c in h1)
