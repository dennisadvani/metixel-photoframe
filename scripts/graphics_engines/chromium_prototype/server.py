#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Standalone stdlib HTTP server for the Chromium kiosk prototype.

Serves the kiosk page (``index.html`` + static assets), the test media folder,
and a ``POST /api/benchmark`` collector that the page's ``benchmark.js`` uses
to report measured FPS.

Deliberately uses ONLY the Python standard library so the prototype stays
completely decoupled from the metixel app (no Flask, no pi3d, no VLC).

Usage:
    python scripts/chromium_prototype/server.py [--port 8000] [--media DIR]

Defaults:
    --media  data/media/sample_media   (relative to the repo root)
    --port   8000
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

# ── Paths ──────────────────────────────────────────────────────────────────

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
DEFAULT_MEDIA = REPO_ROOT / "data" / "media" / "sample_media"

# Static assets served from this directory.
STATIC_DIR = HERE

# Media files served from this directory (images + videos).
MEDIA_DIR: Path = DEFAULT_MEDIA

# Benchmark results collected via POST /api/benchmark.
RESULTS: list[dict] = []
RESULTS_LOCK = threading.Lock()
RESULTS_FILE = HERE / "benchmark_results.json"

# ── MIME types ─────────────────────────────────────────────────────────────

mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("image/jpeg", ".jpg")
mimetypes.add_type("image/jpeg", ".jpeg")
mimetypes.add_type("image/png", ".png")
mimetypes.add_type("image/webp", ".webp")


def _media_listing() -> list[dict]:
    """Return a JSON-serialisable list of media files in MEDIA_DIR."""
    items: list[dict] = []
    if not MEDIA_DIR.is_dir():
        return items
    for p in sorted(MEDIA_DIR.iterdir()):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            kind = "image"
        elif ext in (".mp4", ".webm", ".mov", ".mkv"):
            kind = "video"
        else:
            continue
        items.append({"name": p.name, "url": f"/media/{p.name}", "kind": kind})
    return items


class KioskHandler(BaseHTTPRequestHandler):
    """HTTP handler for the kiosk prototype."""

    # Silence the default request logging (keeps output clean).
    def log_message(self, fmt: str, *args: object) -> None:
        if os.environ.get("METIXEL_PROTO_VERBOSE"):
            super().log_message(fmt, *args)

    # -- Helpers ------------------------------------------------------------

    def _send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj: object, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self._send_bytes(body, "application/json", status)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        with open(path, "rb") as f:
            data = f.read()
        self._send_bytes(data, ctype)

    # -- Routes -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "index.html")
            return

        if path == "/media":
            self._send_json({"items": _media_listing()})
            return

        if path.startswith("/media/"):
            name = unquote(path[len("/media/"):])
            # Guard against path traversal.
            target = (MEDIA_DIR / name).resolve()
            if not str(target).startswith(str(MEDIA_DIR.resolve())):
                self._send_json({"error": "forbidden"}, 403)
                return
            self._send_file(target)
            return

        if path == "/api/benchmark":
            self._send_json({"results": list(RESULTS)})
            return

        # Static assets (style.css, app.js, benchmark.js).
        if path.startswith("/static/"):
            name = unquote(path[len("/static/"):])
            target = (STATIC_DIR / name).resolve()
            if not str(target).startswith(str(STATIC_DIR.resolve())):
                self._send_json({"error": "forbidden"}, 403)
                return
            self._send_file(target)
            return

        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        if self.path.split("?", 1)[0] != "/api/benchmark":
            self._send_json({"error": "not found"}, 404)
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON"}, 400)
            return

        payload["received_at"] = datetime.now(timezone.utc).isoformat()
        with RESULTS_LOCK:
            RESULTS.append(payload)
            _persist_results()

        self._send_json({"ok": True, "count": len(RESULTS)})


def _persist_results() -> None:
    """Atomically write the collected results to disk."""
    tmp = RESULTS_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
        os.replace(tmp, RESULTS_FILE)
    except OSError:
        pass


def _load_results() -> None:
    """Load previously collected results from disk (if any)."""
    global RESULTS
    if RESULTS_FILE.is_file():
        try:
            RESULTS = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            RESULTS = []


def main() -> int:
    global MEDIA_DIR

    parser = argparse.ArgumentParser(description="Chromium kiosk prototype server")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--media",
        type=Path,
        default=DEFAULT_MEDIA,
        help="Directory of test media (images + videos)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default 0.0.0.0 so the Pi can reach it)",
    )
    args = parser.parse_args()

    MEDIA_DIR = args.media.resolve()
    if not MEDIA_DIR.is_dir():
        print(f"ERROR: media directory not found: {MEDIA_DIR}", file=sys.stderr)
        return 1

    _load_results()

    httpd = ThreadingHTTPServer((args.host, args.port), KioskHandler)
    print(f"Metixel Chromium prototype server on http://{args.host}:{args.port}")
    print(f"  Media dir : {MEDIA_DIR}")
    print(f"  Media items: {len(_media_listing())}")
    print("  Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())