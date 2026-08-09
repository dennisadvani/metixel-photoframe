#!/usr/bin/env python3
"""Pre-transcode videos for Metixel on a fast workstation.

Reads videos from a local directory or pulls them from a Pi via SSH,
applies the same transcode logic as VideoProcessor, and writes cached
files to a local output directory.  Results can be pushed back to the
Pi automatically.

Usage:
    # Local media (copy files to workstation first, or use a mapped drive)
    python scripts/precache_videos.py --profile pi2 --media ./local_media --out ./cache

    # Pull from Pi via SSH, process, push results back
    python scripts/precache_videos.py --profile pi2 --host 192.168.222.143 --remote-media /opt/metixel/media/sample_media --out ./cache --push

This produces:
    cache/videos/<hash>.mp4       — transcoded video
    cache/videos/<hash>.1.frame.jpg   — first frame JPEG
    cache/videos/<hash>.2.frame.jpg   — last frame JPEG
    cache/thumbnails/<hash>.jpg   — thumbnail JPEG
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Transcoding profiles (synced with metixel/backend/processing/video.py)
# ---------------------------------------------------------------------------

PROFILES: dict[str, dict[str, Any]] = {
    "pi2": {
        "label": "Raspberry Pi 2",
        "codec": "h264", "encoder": "libx264",
        "max_width": 1920, "max_height": 1080, "max_fps": 30,
        "max_bitrate": 7, "crf": 28,
        "h264_profile": "high", "h264_level": "4.0",
        "color_depth": 8, "hdr_support": False,
    },
    "pi3": {
        "label": "Raspberry Pi 3 / 3B+",
        "codec": "h264", "encoder": "libx264",
        "max_width": 1920, "max_height": 1080, "max_fps": 30,
        "max_bitrate": 7, "crf": 28,
        "h264_profile": "high", "h264_level": "4.0",
        "color_depth": 8, "hdr_support": False,
    },
    "pi4": {
        "label": "Raspberry Pi 4 / 400",
        "codec": "h265", "encoder": "libx265",
        "max_width": 3840, "max_height": 2160, "max_fps": 60,
        "max_bitrate": 40, "crf": 23,
        "h264_profile": "high", "h264_level": "5.1",
        "color_depth": 10, "hdr_support": True,
    },
    "pi5": {
        "label": "Raspberry Pi 5",
        "codec": "h265", "encoder": "libx265",
        "max_width": 3840, "max_height": 2160, "max_fps": 60,
        "max_bitrate": 80, "crf": 23,
        "h264_profile": "high", "h264_level": "5.2",
        "color_depth": 10, "hdr_support": True,
    },
}

H264_CODECS = {"h264", "avc", "avc1", "h.264", "avc1."}
HEVC_CODECS = {"hevc", "h265", "h.265", "hev1", "hvc1"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg"}

# ---------------------------------------------------------------------------
# Helpers — mirrors metixel/backend/processing/video.py
# ---------------------------------------------------------------------------


def hash_file(path: Path) -> str:
    """Content hash: first 1 MB + last 1 KB → SHA‑256 → first 16 hex chars."""
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        sha.update(f.read(1024 * 1024))
        f.seek(-1024, os.SEEK_END)
        sha.update(f.read(1024))
    return sha.hexdigest()[:16]


def probe(path: Path) -> dict[str, Any]:
    """Probe video metadata via ffprobe (mirrors VideoProcessor._probe)."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    data = json.loads(result.stdout)

    info: dict[str, Any] = {
        "width": 0, "height": 0, "duration": 0.0,
        "codec_name": "", "fps": 0.0, "bitrate": 0,
        "color_depth": 8, "h264_profile": "", "h264_level": "",
        "color_primaries": "", "color_trc": "", "colorspace": "",
        "pix_fmt": "",
    }

    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            info["width"] = stream.get("width", 0)
            info["height"] = stream.get("height", 0)
            info["codec_name"] = stream.get("codec_name", "")
            info["pix_fmt"] = stream.get("pix_fmt", "")
            info["h264_profile"] = stream.get("profile", "")
            raw_level = stream.get("level", "")
            if isinstance(raw_level, int) and raw_level > 9:
                info["h264_level"] = float(raw_level) / 10.0
            elif raw_level:
                try:
                    info["h264_level"] = float(raw_level)
                except (ValueError, TypeError):
                    info["h264_level"] = ""
            else:
                info["h264_level"] = ""
            info["color_primaries"] = stream.get("color_primaries", "")
            info["color_trc"] = stream.get("color_transfer", "")
            info["colorspace"] = stream.get("color_space", "")

            fps_str = stream.get("r_frame_rate", "0/1")
            try:
                num, den = fps_str.split("/")
                info["fps"] = round(float(num) / float(den), 2)
            except (ValueError, ZeroDivisionError):
                info["fps"] = 0.0

            if stream.get("bit_rate"):
                info["bitrate"] = int(stream["bit_rate"]) // 1_000_000
            break

    fmt = data.get("format", {})
    if info["bitrate"] == 0 and fmt.get("bit_rate"):
        info["bitrate"] = int(fmt["bit_rate"]) // 1_000_000
    info["duration"] = float(fmt.get("duration", 0))

    pf = info["pix_fmt"]
    if pf and "10" in pf:
        info["color_depth"] = 10
    elif pf and "12" in pf:
        info["color_depth"] = 12
    return info


def needs_optimisation(probe_info: dict, profile: dict) -> bool:
    """Check whether a video needs transcoding (mirrors VideoProcessor.needs_optimisation)."""
    w = probe_info.get("width", 0) or 0
    h = probe_info.get("height", 0) or 0
    if w <= 0 or h <= 0:
        return True

    target_codec = profile.get("codec", "h264")
    source_codec = (probe_info.get("codec_name", "") or "").lower()

    if target_codec == "h264" and source_codec not in H264_CODECS:
        print(f"  → needs transcode: codec {source_codec} not H.264")
        return True
    if target_codec == "h265" and source_codec not in HEVC_CODECS:
        print(f"  → needs transcode: codec {source_codec} not HEVC")
        return True

    max_w = profile.get("max_width", 0)
    if max_w and w > max_w:
        print(f"  → needs transcode: width {w} > max {max_w}")
        return True
    max_h = profile.get("max_height", 0)
    if max_h and h > max_h:
        print(f"  → needs transcode: height {h} > max {max_h}")
        return True

    max_fps = profile.get("max_fps", 0)
    src_fps = probe_info.get("fps", 0) or 0
    if max_fps and src_fps > max_fps:
        print(f"  → needs transcode: fps {src_fps:.2f} > max {max_fps}")
        return True

    max_br = profile.get("max_bitrate", 0)
    src_br = probe_info.get("bitrate", 0) or 0
    if max_br and src_br > int(max_br * 1.1):
        print(f"  → needs transcode: bitrate {src_br} Mbps > max {max_br} (+10%={int(max_br * 1.1)})")
        return True

    target_depth = profile.get("color_depth", 8)
    src_depth = probe_info.get("color_depth", 8) or 8
    if src_depth > target_depth:
        print(f"  → needs transcode: color depth {src_depth} > target {target_depth}")
        return True

    if not profile.get("hdr_support", False) and probe_info.get("color_trc", ""):
        trc = probe_info["color_trc"]
        if trc in ("smpte2084", "arib-std-b67", "smpte428", "bt2020-10"):
            print(f"  → needs transcode: HDR source on non-HDR Pi (trc={trc})")
            return True

    if target_codec == "h264" and source_codec in H264_CODECS:
        target_level = profile.get("h264_level", "")
        src_level = probe_info.get("h264_level", "")
        if target_level != "" and src_level != "":
            try:
                if float(src_level) > float(target_level):
                    print(f"  → needs transcode: H.264 level {src_level} > target {target_level}")
                    return True
            except (ValueError, TypeError):
                pass

    return False


def transcode(source: Path, dest: Path, profile: dict, info: dict) -> None:
    """Transcode a video using the profile settings (mirrors VideoProcessor._transcode)."""
    target_encoder = profile.get("encoder", "libx264")
    h264_level = str(profile.get("h264_level", ""))
    h264_profile = profile.get("h264_profile", "high")
    color_depth = profile.get("color_depth", 8)
    hdr_support = profile.get("hdr_support", False)
    max_w = profile.get("max_width", 1920)
    max_h = profile.get("max_height", 1080)
    max_fps = profile.get("max_fps", 0)
    crf = profile.get("crf", 23)

    scale_filter = (
        f"scale='min({max_w},iw)':'min({max_h},ih)'"
        f":force_original_aspect_ratio=decrease"
        f",pad='ceil(iw/2)*2:ceil(ih/2)*2:(ow-iw)/2:(oh-ih)/2'"
    )
    src_depth = (info or {}).get("color_depth", 8) or 8
    out_depth = min(src_depth, color_depth)
    scale_filter += ",format=yuv420p10le" if out_depth >= 10 else ",format=yuv420p"

    encoders = [target_encoder]
    if "libx264" not in encoders:
        encoders.append("libx264")

    for encoder in encoders:
        cmd: list[str] = [
            "ffmpeg", "-y", "-i", str(source),
            "-c:v", encoder, "-vf", scale_filter,
        ]

        if encoder in ("libx264", "libx265"):
            cmd += ["-preset", "fast", "-crf", str(crf)]
        else:
            cmd += ["-b:v", "2M"]

        if encoder == "libx264" and h264_level:
            cmd += ["-level", h264_level]
        if encoder == "libx264" and h264_profile:
            cmd += ["-profile:v", h264_profile]
        cmd += ["-refs", "2", "-g", "30"]

        src_fps = (info or {}).get("fps", 0) or 0
        if src_fps > 0:
            target_fps = min(src_fps, max_fps) if max_fps else src_fps
            cmd += ["-r", str(target_fps)]

        cmd += ["-an"]

        if not hdr_support:
            cmd += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]

        max_br = profile.get("max_bitrate", 0)
        src_br = (info or {}).get("bitrate", 0) or 0
        if max_br and max_br > 0:
            effective_max = min(src_br, max_br) if src_br else max_br
            cmd += ["-maxrate", f"{effective_max}M", "-bufsize", f"{effective_max * 2}M"]

        cmd += ["-movflags", "+faststart", str(dest)]

        print(f"  ffmpeg: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True, timeout=7200)
            print(f"  → transcoded: {dest.name}")
            return
        except subprocess.CalledProcessError as e:
            print(f"  encoder {encoder} failed (rc={e.returncode}), trying next…")
            if dest.exists():
                dest.unlink()
        except subprocess.TimeoutExpired:
            print(f"  encoder {encoder} timed out, trying next…")
            if dest.exists():
                dest.unlink()

    raise RuntimeError(f"All encoders failed for: {source.name}")


def extract_frame(src: Path, dest: Path, seek: str, max_w: int = 0, max_h: int = 0, timeout: int = 120) -> bool:
    """Extract a single frame, downscaling if larger than max_w x max_h."""
    cmd: list[str] = ["ffmpeg", "-y"]
    vf_parts: list[str] = []
    if max_w and max_h:
        vf_parts.append(f"scale='min({max_w},iw)':'min({max_h},ih)':force_original_aspect_ratio=decrease")
        vf_parts.append(f"pad='ceil(iw/2)*2:ceil(ih/2)*2:(ow-iw)/2:(oh-ih)/2'")
    if seek == "0":
        cmd += ["-noaccurate_seek", "-ss", "0"]
    else:
        cmd += ["-sseof", "-1"]
    cmd += ["-i", str(src)]
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
    # -vframes is an output option — must come after -i
    cmd += ["-vframes", "1", "-q:v", "2", "-f", "mjpeg"]
    if seek != "0":
        cmd += ["-update", "1"]
    cmd.append(str(dest))
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        return True
    except Exception:
        return False


def extract_thumbnail(src: Path, dest: Path, max_w: int = 0, max_h: int = 0) -> bool:
    """Extract a thumbnail frame at t=2s, downscaling if larger than max_w x max_h."""
    cmd = ["ffmpeg", "-y", "-noaccurate_seek", "-ss", "2"]
    cmd += ["-i", str(src)]
    if max_w and max_h:
        vf = (f"scale='min({max_w},iw)':'min({max_h},ih)':force_original_aspect_ratio=decrease,"
              f"pad='ceil(iw/2)*2:ceil(ih/2)*2:(ow-iw)/2:(oh-ih)/2'")
        cmd += ["-vf", vf]
    cmd += ["-vframes", "1", "-q:v", "2", str(dest)]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------


def ssh_run(host: str, cmd: str) -> str:
    """Run a command on a Pi via SSH and return stdout."""
    result = subprocess.run(
        ["ssh", f"pi@{host}", cmd],
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip()


def scp_pull(host: str, remote_path: str, local_dir: Path) -> None:
    """Pull files from a Pi to a local directory via scp."""
    subprocess.run(
        ["scp", "-r", f"pi@{host}:{remote_path}", str(local_dir)],
        check=True, timeout=300,
    )


def scp_push(local_dir: Path, host: str, remote_dir: str) -> None:
    """Push files from a local directory to a Pi via scp."""
    subprocess.run(
        ["scp", "-r", f"{local_dir}/*", f"pi@{host}:{remote_dir}/"],
        check=True, timeout=300,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-transcode videos for Metixel")
    parser.add_argument("--profile", required=True, choices=list(PROFILES), help="Target Pi model profile")
    parser.add_argument("--media", help="Path to local media directory")
    parser.add_argument("--host", help="Pi hostname/IP to pull media from via SSH")
    parser.add_argument("--remote-media", default="/opt/metixel/media/sample_media", help="Remote media path on Pi (default: /opt/metixel/media/sample_media)")
    parser.add_argument("--out", default="./cache", help="Output cache directory (default: ./cache)")
    parser.add_argument("--push", action="store_true", help="Push results back to Pi via SCP after processing")
    parser.add_argument("--force", action="store_true", help="Re-transcode even if output exists")
    args = parser.parse_args()

    if not args.media and not args.host:
        parser.error("Either --media or --host is required")

    profile = PROFILES[args.profile]
    max_w = profile["max_width"]
    max_h = profile["max_height"]
    out_dir = Path(args.out)
    video_dir = out_dir / "videos"
    thumb_dir = out_dir / "thumbnails"
    video_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    print(f"Profile: {profile['label']} ({args.profile})")
    print(f"Limits:  {profile['max_width']}x{profile['max_height']} @ {profile['max_fps']}fps, "
          f"{profile['max_bitrate']} Mbps, {profile['codec'].upper()}, CRF {profile['crf']}")
    print()

    # -- Resolve media directory --------------------------------------------
    media_dir: Path
    cleanup_media: bool = False

    if args.host:
        print(f"Pulling media from pi@{args.host}:{args.remote_media} …")
        media_dir = Path(tempfile.mkdtemp(prefix="metixel_media_"))
        cleanup_media = True
        scp_pull(args.host, f"{args.remote_media}/*.mp4", media_dir)
        # Also pull any other video extensions
        for ext in [".mov", ".mkv", ".avi", ".webm", ".m4v"]:
            try:
                scp_pull(args.host, f"{args.remote_media}/*{ext}", media_dir)
            except subprocess.CalledProcessError:
                pass  # No files with that extension
        print(f"  → pulled to {media_dir}")
    else:
        media_dir = Path(args.media)

    print(f"Media:   {media_dir}")
    print(f"Output:  {out_dir}")
    print()

    # -- Find videos --------------------------------------------------------
    videos = sorted(
        media_dir / Path(p) for p in os.listdir(str(media_dir))
        if Path(p).suffix.lower() in VIDEO_EXTENSIONS
    )
    if not videos:
        print("No video files found.")
        if cleanup_media:
            shutil.rmtree(media_dir, ignore_errors=True)
        return

    print(f"Found {len(videos)} video(s)\n")

    # -- Process each video -------------------------------------------------
    for i, src in enumerate(videos, 1):
        print(f"[{i}/{len(videos)}] {src.name}")
        file_hash = hash_file(src)

        cached_mp4 = video_dir / f"{file_hash}.mp4"
        first_frame = video_dir / f"{file_hash}.1.frame.jpg"
        last_frame = video_dir / f"{file_hash}.2.frame.jpg"
        thumb = thumb_dir / f"{file_hash}.jpg"

        # Probe source
        info = probe(src)
        print(f"  source: {info['width']}x{info['height']}, {info['fps']:.2f}fps, "
              f"{info['bitrate']} Mbps, {info['codec_name']}, level={info['h264_level']}")

        # Check if transcode needed
        if not needs_optimisation(info, profile):
            print(f"  → already optimal — skipping transcode")
        elif cached_mp4.exists() and not args.force:
            print(f"  → cached: {cached_mp4.name} (use --force to re-transcode)")
        else:
            print(f"  transcoding…")
            transcode(src, cached_mp4, profile, info)

        # Extract thumbnail
        if not thumb.exists() or args.force:
            print(f"  extracting thumbnail…")
            if extract_thumbnail(src, thumb, max_w, max_h):
                print(f"  → thumbnail: {thumb.name}")
            else:
                print(f"  ✗ thumbnail failed")

        # Extract frames
        if not first_frame.exists() or args.force:
            print(f"  extracting first frame…")
            if extract_frame(src, first_frame, "0", max_w, max_h):
                print(f"  → first frame: {first_frame.name}")
            else:
                print(f"  ✗ first frame failed")
        else:
            print(f"  → first frame cached: {first_frame.name}")

        if not last_frame.exists() or args.force:
            print(f"  extracting last frame…")
            if extract_frame(src, last_frame, "-1", max_w, max_h, timeout=120):
                print(f"  → last frame: {last_frame.name}")
            else:
                print(f"  ✗ last frame failed")
        else:
            print(f"  → last frame cached: {last_frame.name}")

        print()

    # -- Cleanup pulled media -----------------------------------------------
    if cleanup_media:
        shutil.rmtree(media_dir, ignore_errors=True)
        print("Cleaned up temporary media files.")

    # -- Push results back to Pi --------------------------------------------
    if args.push and args.host:
        print(f"\nPushing results to pi@{args.host} …")
        # Stop backend before pushing to avoid partial reads
        ssh_run(args.host, "sudo systemctl stop metixel-cage metixel-backend 2>/dev/null || true")
        scp_push(video_dir, args.host, "/opt/metixel/cache/videos")
        scp_push(thumb_dir, args.host, "/opt/metixel/cache/thumbnails")
        ssh_run(args.host, "sudo systemctl start metixel-backend metixel-cage")
        print("  → pushed and backend restarted")

    print("\nDone.")
    if not args.push:
        print()
        print("To deploy to the Pi:")
        print(f"  scp {video_dir}/*.mp4 {video_dir}/*.frame pi@<ip>:/opt/metixel/cache/videos/")
        print(f"  scp {thumb_dir}/*.jpg pi@<ip>:/opt/metixel/cache/thumbnails/")


if __name__ == "__main__":
    main()
