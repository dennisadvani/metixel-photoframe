# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Image optimisation worker — runs in a subprocess with nice/ionice isolation.

This module is a standalone CLI, not imported as a library.  It is spawned
by ``ImageProcessor.process()`` via ``subprocess.run()`` so that the
memory-hungry PIL operations (loading 6000×4000+ JPEGs, resizing,
thumbnail generation) happen in a separate OS process that can be:

* **nice'd** (``nice -n 19``) — lowest CPU priority, won't starve Flask
* **ionice'd** — lowest I/O priority, won't starve the frontend
* **CPU-limited** via ``cpulimit`` — hard cap on CPU usage
* **Memory-isolated** — the subprocess exits and the kernel reclaims
  every byte, eliminating any risk of Python heap fragmentation in the
  long-running backend daemon.

Usage (called by ImageProcessor)::

    nice -n 19 python3 -m metixel.backend.processing.worker \\
        --source /opt/metixel/media/sync/immich/img.jpg \\
        --cache /opt/metixel/cache/images/abc123.jpg \\
        --thumb /opt/metixel/cache/thumbnails/abc123.jpg \\
        --screen 1920x1200

Output (stdout)::

    {"status": "ok", "id": "abc123", "width": 1721, "height": 1296,
     "exif": {"Orientation": "1", "DateTime": "2024-01-01 12:00:00"}}

Exit codes:
    0 — success, image processed or already cached
    1 — corrupt/unreadable image (caller should delete the source)
    2 — transient error (I/O, permissions — caller may retry or skip)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

JPEG_QUALITY = 85
THUMBNAIL_SIZE = 320


def _hash_file(path: Path) -> str:
    """Compute SHA-256 hash of a file (first 1 MB for speed)."""
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        sha.update(f.read(1024 * 1024))
        f.seek(-1024, 2)
        sha.update(f.read(1024))
    return sha.hexdigest()[:16]


def _validate_cached(path: Path) -> bool:
    """Check that a cached JPEG is readable (not corrupt/truncated)."""
    try:
        from PIL import Image as PILImage

        with PILImage.open(path) as img:
            img.verify()
        return True
    except Exception:
        return False


def _read_exif(source_path: Path) -> dict[str, str]:
    """Read EXIF tags without fully decoding the image."""
    try:
        from PIL import Image as PILImage
        from PIL.ExifTags import TAGS

        with PILImage.open(source_path) as img:
            exif = img.getexif()
            if exif:
                return {
                    TAGS.get(k, str(k)): str(v) for k, v in exif.items() if not isinstance(v, bytes)
                }
    except Exception:
        pass
    return {}


def _process(args: argparse.Namespace) -> dict:
    """Main processing logic.  Returns a JSON-serialisable result dict."""
    source = Path(args.source)
    cached = Path(args.cache)
    thumb = Path(args.thumb)
    screen_w, screen_h = args.screen

    # ── Compute content hash ───────────────────────────────────────
    file_hash = _hash_file(source)

    # ── Cache hit ───────────────────────────────────────────────────
    if cached.exists():
        if cached.stat().st_size < 1024 or not _validate_cached(cached):
            cached.unlink(missing_ok=True)
        else:
            # Regenerate thumbnail if missing
            if not thumb.exists():
                _regenerate_thumbnail(cached, thumb)
            exif = _read_exif(source)
            w, h = _get_dimensions(cached)
            return {
                "status": "ok",
                "id": file_hash,
                "width": w,
                "height": h,
                "exif": exif,
                "cached": True,
            }

    # ── Load, transform, resize, save ───────────────────────────────
    from PIL import Image as PILImage
    from PIL import ImageOps, UnidentifiedImageError

    try:
        with PILImage.open(source) as img:
            # Auto-rotate based on EXIF orientation
            img = ImageOps.exif_transpose(img)

            # Extract EXIF before mode conversion
            exif: dict[str, str] = {}
            try:
                raw = img.getexif()
                if raw:
                    from PIL.ExifTags import TAGS

                    exif = {
                        TAGS.get(k, str(k)): str(v)
                        for k, v in raw.items()
                        if not isinstance(v, bytes)
                    }
            except Exception:
                pass

            # Convert to RGB — composite transparent images onto black
            # first so alpha areas don't render as white.
            if img.mode in ("RGBA", "PA"):
                bg = PILImage.new("RGB", img.size, (0, 0, 0))
                bg.paste(img, img)
                img = bg
            elif img.mode not in ("RGB", "L"):
                img = img.convert("RGB")

            # Resize to screen resolution (maintain aspect ratio)
            max_w = int(screen_w * 1.2)
            max_h = int(screen_h * 1.2)
            img.thumbnail((max_w, max_h), PILImage.LANCZOS)

            # Save cached version
            cached.parent.mkdir(parents=True, exist_ok=True)
            img.save(cached, "JPEG", quality=JPEG_QUALITY)
            w, h = img.size

            # Generate thumbnail
            thumb.parent.mkdir(parents=True, exist_ok=True)
            t = img.copy()
            t.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE), PILImage.LANCZOS)
            t.save(thumb, "JPEG", quality=70)

        return {
            "status": "ok",
            "id": file_hash,
            "width": w,
            "height": h,
            "exif": exif,
            "cached": False,
        }

    except UnidentifiedImageError:
        return {"status": "corrupt", "id": file_hash}
    except OSError as e:
        return {"status": "error", "id": file_hash, "message": str(e)}


def _regenerate_thumbnail(cached: Path, thumb: Path) -> None:
    """Generate a 320 px thumbnail from an already-cached image."""
    try:
        from PIL import Image as PILImage

        with PILImage.open(cached) as img:
            t = img.copy()
            t.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE), PILImage.LANCZOS)
            thumb.parent.mkdir(parents=True, exist_ok=True)
            t.save(thumb, "JPEG", quality=70)
    except Exception:
        pass  # Non-fatal — slideshow works without thumbnails


def _get_dimensions(path: Path) -> tuple[int, int]:
    """Read image dimensions without decoding pixel data."""
    try:
        from PIL import Image as PILImage

        with PILImage.open(path) as img:
            return img.size
    except Exception:
        return (0, 0)


# ── CLI entry point ────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Metixel image optimisation worker")
    parser.add_argument("--source", required=True, help="Path to source image")
    parser.add_argument("--cache", required=True, help="Path for cached JPEG")
    parser.add_argument("--thumb", required=True, help="Path for thumbnail JPEG")
    parser.add_argument("--screen", required=True, help="Screen resolution as WxH (e.g. 1920x1200)")
    args = parser.parse_args()

    # Parse screen resolution
    try:
        w, h = args.screen.split("x")
        args.screen = (int(w), int(h))
    except (ValueError, AttributeError):
        print(json.dumps({"status": "error", "message": f"Invalid --screen: {args.screen}"}))
        sys.exit(2)

    result = _process(args)

    # Always print JSON to stdout
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()

    # Exit code signals the nature of the failure
    status = result.get("status", "error")
    if status == "corrupt":
        sys.exit(1)  # Caller should delete the source file
    elif status == "error":
        sys.exit(2)  # Transient error — caller may retry
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
