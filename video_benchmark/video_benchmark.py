#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Interactive video playback benchmark for Raspberry Pi.

Transcodes a source video locally into a single test file from your chosen
settings, copies it to a target Pi, plays it fullscreen with VLC, and measures
the actual playback frame rate via VLC's RC interface.

The goal is to let you change ONE variable at a time and use deduction to find
the transcode profile that plays smoothly on a given Pi — rather than building
a huge matrix of test files.

Usage:
    python video_benchmark/video_benchmark.py --user pi --ip 192.168.222.230

The target Pi is specified with ``--user`` and ``--ip`` (or ``--host``).
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────

SOURCE_DIR = Path(__file__).resolve().parent / "source"
OUT_DIR = Path(__file__).resolve().parent / "out"
REMOTE_DIR = "/tmp/metixel-bench"

# ── Interactive option tables ──────────────────────────────────────────────

RESOLUTIONS = {
    "1": ("4K", "2160p", 3840, 2160),
    "2": ("2K", "1440p", 2560, 1440),
    "3": ("1080p", "1080p", 1920, 1080),
    "4": ("720p", "720p", 1280, 720),
}

FRAME_RATES = {
    "1": (60, "60fps"),
    "2": (30, "30fps"),
    "3": (24, "24fps"),
}

BITRATES = {
    "1": (10, "10mbps"),
    "2": (8, "8mbps"),
    "3": (6, "6mbps"),
}

AUDIO = {
    "1": (False, "noaudio"),
    "2": (True, "aac"),
}

CODECS = {
    "1": ("h264", "h264"),
    "2": ("hevc", "hevc"),
}

PIX_FMTS = {
    "1": ("yuv420p", "yuv420p"),
    "2": ("yuvj420p", "yuvj420p"),
}

# H.264 profile/level options (only used when codec == h264)
H264_PROFILES = {
    "1": ("high", "4.0", "high"),
    "2": ("main", "4.0", "main"),
    "3": ("baseline", "3.0", "baseline"),
}

B_FRAMES = {
    "1": (0, "bf0"),
    "2": (None, "bfdefault"),
}


# ── Pi config ──────────────────────────────────────────────────────────────


@dataclass
class PiConfig:
    """The target Pi to benchmark against."""

    user: str
    ip: str

    @property
    def ssh_target(self) -> str:
        return f"{self.user}@{self.ip}"


# ── Interactive prompts ────────────────────────────────────────────────────


def _pick(prompt: str, options: dict[str, tuple], default: str = "1") -> tuple:
    """Show a numbered menu and return the selected option tuple."""
    print(f"\n{prompt}")
    for key, (label, *_) in options.items():
        marker = " (default)" if key == default else ""
        print(f"  [{key}] {label}{marker}")
    choice = input(f"  Select [{default}]: ").strip() or default
    if choice not in options:
        print(f"  Invalid choice '{choice}', using default.")
        choice = default
    return options[choice]


# ── Local transcode ────────────────────────────────────────────────────────


def build_ffmpeg_cmd(
    source: Path,
    out_path: Path,
    res: tuple,
    fps: int,
    bitrate_mbps: int,
    with_audio: bool,
    codec: str,
    pix_fmt: str,
    h264_profile: str | None,
    h264_level: str | None,
    b_frames: int | None,
) -> list[str]:
    """Build the ffmpeg command for the selected settings."""
    width, height = res[2], res[3]
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
        "-r",
        str(fps[0]),
        "-b:v",
        f"{bitrate_mbps}M",
        "-maxrate",
        f"{bitrate_mbps}M",
        "-bufsize",
        f"{bitrate_mbps * 2}M",
        "-pix_fmt",
        pix_fmt,
    ]

    if codec == "h264":
        cmd += ["-c:v", "libx264", "-profile:v", h264_profile, "-level", h264_level]
        if b_frames is not None:
            cmd += ["-bf", str(b_frames)]
    else:
        cmd += ["-c:v", "libx265", "-x265-params", f"bframes={b_frames or 0}"]

    if with_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]

    # Only transcode the first 10 seconds — enough to benchmark playback
    # without spending time encoding the whole source.
    cmd += ["-t", "10", "-movflags", "+faststart", str(out_path)]
    return cmd


def transcode(source: Path, out_path: Path, settings: dict) -> None:
    """Run ffmpeg to produce the test file.

    Skips transcoding if the output file already exists (so re-running the
    same settings reuses the previous result instead of re-encoding).
    """
    if out_path.exists():
        print(f"\n[transcode] Skipping — {out_path.name} already exists "
              f"({out_path.stat().st_size / 1e6:.1f} MB)")
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_ffmpeg_cmd(source, out_path, **settings)
    print(f"\n[transcode] ffmpeg {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[transcode] FAILED:\n{result.stderr[-2000:]}")
        sys.exit(1)
    print(f"[transcode] OK → {out_path.name} ({out_path.stat().st_size / 1e6:.1f} MB)")


# ── Remote helpers ─────────────────────────────────────────────────────────


def ssh(pi: PiConfig, command: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a command on the Pi over SSH."""
    return subprocess.run(
        ["ssh", pi.ssh_target, command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def scp_to(pi: PiConfig, local: Path, remote: str) -> None:
    """Copy a local file to the Pi."""
    subprocess.run(
        ["scp", str(local), f"{pi.ssh_target}:{remote}"],
        check=True,
        capture_output=True,
        text=True,
    )


def crop_ratio(display_size: str) -> str:
    """Return a VLC ``--crop``/``--aspect-ratio`` string (W:H) for a display.

    Mirrors the app's ``VlcVideoPlayer._compute_crop_ratio``: reduce the
    display resolution to its simplest integer ratio (e.g. "16:9").
    """
    from math import gcd

    w_str, _, h_str = display_size.lower().partition("x")
    try:
        w, h = int(w_str), int(h_str)
    except ValueError:
        return "16:9"
    g = gcd(w, h)
    return f"{w // g}:{h // g}"


def open_tunnel(pi: PiConfig, port: int) -> subprocess.Popen:
    """Open an SSH local-port-forward tunnel from this workstation to the Pi.

    VLC listens on the Pi's ``localhost:<port>`` — this tunnel forwards the
    workstation's own ``localhost:<port>`` to it, so the RC measurement can
    connect over 127.0.0.1 locally.
    """
    cmd = [
        "ssh",
        "-N",
        "-L",
        f"127.0.0.1:{port}:127.0.0.1:{port}",
        pi.ssh_target,
    ]
    print(f"[tunnel] ssh -L 127.0.0.1:{port}:127.0.0.1:{port} {pi.ssh_target}")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Wait a moment for the tunnel to establish
    time.sleep(1.0)
    return proc


def close_tunnel(proc: subprocess.Popen | None) -> None:
    """Terminate an open SSH tunnel process."""
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


# ── VLC RC measurement ─────────────────────────────────────────────────────


def _query_rc(port: int, command: str, timeout: float = 0.5) -> str:
    """Send a command to VLC's RC TCP interface and return the full response.

    Reads until the ``> `` prompt reappears (the command output is complete),
    so multi-line responses like ``stats`` are captured in full.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(("127.0.0.1", port))
        banner = b""
        while b"> " not in banner:
            chunk = sock.recv(256)
            if not chunk:
                raise OSError("VLC closed RC connection during banner")
            banner += chunk
        sock.sendall((command + "\n").encode())
        response = b""
        while True:
            try:
                sock.settimeout(0.3)
                chunk = sock.recv(256)
                if not chunk:
                    break
                response += chunk
                # The command output is complete once the prompt returns.
                if b"\n> " in response:
                    break
            except TimeoutError:
                break
    finally:
        sock.close()
    # Strip the trailing prompt and return the full multi-line output.
    text = response.decode(errors="replace")
    if "\n> " in text:
        text = text.split("\n> ", 1)[0]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError(f"Empty RC response for '{command}'")
    return "\n".join(lines)


def measure_fps(
    port: int,
    duration: float = 5.0,
    settle_seconds: float = 3.0,
) -> dict:
    """Sample VLC's stats over a window and compute actual FPS + dropped frames.

    Waits up to 10s for playback to start, then settles for ``settle_seconds``
    (so initial buffering/startup isn't counted) before sampling, and measures
    over a ``duration``-second window.
    """
    # Wait for playback to start
    for _ in range(50):
        try:
            if _query_rc(port, "is_playing") == "1":
                break
        except (OSError, TimeoutError, ValueError):
            pass
        time.sleep(0.2)

    # Let playback settle past any initial startup/buffering
    time.sleep(settle_seconds)

    # Sample start
    start_stats = _query_rc(port, "stats")
    start_displayed = _parse_stat(start_stats, "frames displayed")
    start_lost = _parse_stat(start_stats, "frames lost")
    start_time = time.monotonic()

    time.sleep(duration)

    end_stats = _query_rc(port, "stats")
    end_displayed = _parse_stat(end_stats, "frames displayed")
    end_lost = _parse_stat(end_stats, "frames lost")
    elapsed = time.monotonic() - start_time

    displayed = end_displayed - start_displayed
    lost = end_lost - start_lost
    fps = displayed / elapsed if elapsed > 0 else 0.0
    return fps, displayed, lost


def _parse_stat(stats: str, key: str) -> int:
    """Extract an integer value for ``key`` from VLC's ``stats`` output.

    VLC 3.x reports ``frames displayed`` and ``frames lost`` (not the older
    ``displayed pictures`` / ``lost pictures`` names).
    """
    for line in stats.splitlines():
        if key in line:
            # e.g. "| frames displayed :     4404"
            try:
                return int(line.split(":")[-1].strip())
            except ValueError:
                return 0
    return 0


# ── Main ───────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Source video (default: first file in video_benchmark/source/)",
    )
    parser.add_argument(
        "--measure-seconds",
        type=float,
        default=5.0,
        help="How long to sample VLC stats (default 5s)",
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=3.0,
        help="Delay after playback starts before measuring (default 3s)",
    )
    parser.add_argument(
        "--user",
        default="pi",
        help="SSH username for the Pi (default: pi)",
    )
    parser.add_argument(
        "--ip",
        dest="host",
        default=None,
        help="IP address or hostname of the Pi (required)",
    )
    parser.add_argument(
        "--host",
        dest="host",
        default=None,
        help="Alias for --ip",
    )
    parser.add_argument(
        "--fit-mode",
        default="contain",
        choices=["contain", "cover", "fill"],
        help="How to fit the video to the display: cover (crop, like the app), "
             "contain (letterbox), or fill (stretch). Default: contain",
    )
    parser.add_argument(
        "--display",
        dest="display_size",
        default="1920x1080",
        help="Display resolution as WxH for crop/fill fit modes (default 1920x1080)",
    )
    args = parser.parse_args()

    # ── Pi config ─────────────────────────────────────────────────────
    if not args.host:
        print("[pi] ERROR: specify the Pi with --ip (e.g. --ip 192.168.222.230)")
        sys.exit(1)
    pi = PiConfig(user=args.user, ip=args.host)
    print(f"\n[pi] Using {pi.ssh_target}")

    # ── Source file ───────────────────────────────────────────────────
    source = args.source
    if source is None:
        sources = sorted(SOURCE_DIR.glob("*"))
        if not sources:
            print(f"[source] No files in {SOURCE_DIR}. Drop a test video there.")
            sys.exit(1)
        source = sources[0]
    if not source.exists():
        print(f"[source] Not found: {source}")
        sys.exit(1)
    print(f"[source] {source.name}")

    # ── Interactive settings ──────────────────────────────────────────
    res = _pick("Resolution:", RESOLUTIONS)
    fps = _pick("Frame rate:", FRAME_RATES)
    bitrate = _pick("Bitrate:", BITRATES)
    audio = _pick("Audio:", AUDIO)
    codec = _pick("Codec:", CODECS)
    pix_fmt = _pick("Pixel format:", PIX_FMTS)

    h264_profile = h264_level = None
    if codec[0] == "h264":
        prof = _pick("H.264 profile/level:", H264_PROFILES)
        h264_profile, h264_level = prof[0], prof[1]
    b_frames = _pick("B-frames:", B_FRAMES)[0]

    # ── Build descriptive filename ────────────────────────────────────
    parts = [
        res[1],
        fps[1],
        bitrate[1],
        codec[1],
        pix_fmt[1],
        h264_profile or "hevc",
        f"bf{b_frames}" if b_frames is not None else "bfdefault",
        audio[1],
    ]
    out_name = "_".join(parts) + ".mp4"
    out_path = OUT_DIR / out_name

    settings = {
        "res": res,
        "fps": fps,
        "bitrate_mbps": bitrate[0],
        "with_audio": audio[0],
        "codec": codec[0],
        "pix_fmt": pix_fmt[0],
        "h264_profile": h264_profile,
        "h264_level": h264_level,
        "b_frames": b_frames,
    }

    # ── Transcode locally ─────────────────────────────────────────────
    transcode(source, out_path, settings)

    # ── Copy to Pi ────────────────────────────────────────────────────
    print(f"\n[copy] scp {out_path.name} → {pi.ssh_target}:{REMOTE_DIR}/")
    ssh(pi, f"mkdir -p {REMOTE_DIR}")
    scp_to(pi, out_path, f"{REMOTE_DIR}/{out_path.name}")

    # ── Run VLC on the Pi ─────────────────────────────────────────────
    # Pick a free RC port.
    tmp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tmp.bind(("127.0.0.1", 0))
    rc_port = tmp.getsockname()[1]
    tmp.close()

    remote_path = f"{REMOTE_DIR}/{out_path.name}"
    # Apply the same fit-mode logic as the app's VlcVideoPlayer:
    #   cover → --crop=W:H (CSS cover)
    #   fill  → --aspect-ratio=W:H (stretch)
    #   contain → nothing (letterbox/pillarbox)
    fit_flag = ""
    if args.fit_mode == "cover":
        fit_flag = f" --crop={crop_ratio(args.display_size)}"
    elif args.fit_mode == "fill":
        fit_flag = f" --aspect-ratio={crop_ratio(args.display_size)}"
    vlc_cmd = (
        f"vlc --no-audio --play-and-exit --no-video-title-show "
        f"--intf dummy --extraintf rc --rc-host localhost:{rc_port} "
        f"--fullscreen{fit_flag} {remote_path}"
    )
    print(f"\n[pi] Launching VLC fullscreen on {pi.ssh_target}…")
    print(f"[pi]   {vlc_cmd}")
    ssh(pi, f"nohup {vlc_cmd} >/dev/null 2>&1 &")

    # ── Open SSH tunnel to VLC's RC port ──────────────────────────────
    tunnel = open_tunnel(pi, rc_port)

    # ── Measure ───────────────────────────────────────────────────────
    print(f"\n[measure] Settling {args.settle_seconds:.0f}s, then sampling "
          f"VLC stats for {args.measure_seconds:.0f}s…")
    actual_fps = displayed = lost = None
    try:
        actual_fps, displayed, lost = measure_fps(
            rc_port, args.measure_seconds, args.settle_seconds
        )
    except (OSError, TimeoutError, ValueError) as e:
        print(f"[measure] Could not reach VLC RC: {e}")
        print("[measure] Check VLC is installed and the display is available.")
        print("[measure] Is the SSH tunnel to the Pi reachable?")
        close_tunnel(tunnel)
        sys.exit(1)
    finally:
        close_tunnel(tunnel)

    # ── Report ────────────────────────────────────────────────────────
    nominal = fps[0]
    print("\n" + "=" * 60)
    print("RESULT")
    print("=" * 60)
    print(f"  File:        {out_path.name}")
    print(f"  Nominal fps: {nominal}")
    print(f"  Measured:    {actual_fps:.1f} fps")
    print(f"  Displayed:   {displayed} frames in {args.measure_seconds:.0f}s")
    print(f"  Lost:        {lost} dropped frames")
    ratio = actual_fps / nominal if nominal else 0
    verdict = "SMOOTH" if ratio >= 0.95 else ("MARGINAL" if ratio >= 0.85 else "CHOPPY")
    print(f"  Verdict:     {verdict} ({ratio * 100:.0f}% of nominal)")
    print("=" * 60)
    print("\nVisually inspect the screen. Change ONE setting and re-run to compare.")


if __name__ == "__main__":
    main()
