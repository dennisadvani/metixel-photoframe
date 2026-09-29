# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Screen capture — save the composited frame to a PNG using grim.

Why grim, and why it is driven by the backend
---------------------------------------------
"What is on the panel" is the *compositor's* output, not a Qt widget.  Two
things live outside Qt's paint system and would be missing from a
``QWidget.grab()``:

* the video surface — :class:`~metixel.display.qt_mpv.MpvRenderWidget` is a
  ``QOpenGLWidget`` rendering into its own framebuffer, which ``grab()`` does
  not capture;
* the rotation — applied as a compositor output transform
  (``WlrOutput.set_mode`` → ``wlr-randr --transform``), so Qt always paints
  logical, unrotated coordinates.  A widget grab would need rotating afterwards
  to match what a user actually sees.

``grim`` asks the compositor for the composited output over ``wlr-screencopy``,
so it captures exactly those pixels, rotation and video included.

That does not require running inside the cage session.  Like ``wlr-randr``,
grim is simply another Wayland client, and
:func:`~metixel.display.hardware.wayland_env` supplies the socket it needs — so
the capture is served by the backend, with no IPC round-trip to the frontend.

Failure is always reported, never raised.  A frame on a wall must not blank
because a screenshot could not be taken (``AGENTS.md`` rule 7).
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from metixel.display.hardware import wayland_env
from metixel.shared.paths import resolve_install_path

logger = logging.getLogger(__name__)

#: Path to the grim binary (wlroots screenshot tool, from the ``grim`` package).
_GRIM_BIN = "/usr/bin/grim"

#: How long grim may take.  It is a single compositor round-trip, so it is
#: fast; the bound exists only so a wedged compositor cannot hold the request
#: thread open indefinitely.
CAPTURE_TIMEOUT_S = 15.0

#: Fallback when ``system.screenshot_dir`` is unset.  Resolved relative to the
#: data dir, which puts it inside the media tree on purpose: the existing
#: ``[metixel-media]`` Samba share then exposes it with no second share.
DEFAULT_SCREENSHOT_DIR = "media/screenshots/"

#: Filename stamp.  Sorts chronologically and is readable in a file browser.
_STAMP_FORMAT = "%Y%m%d-%H%M%S"


@dataclass(frozen=True)
class CaptureResult:
    """Outcome of one capture attempt.

    ``error`` is user-facing: the route passes it straight to the dashboard
    toast, so it is written to be read by a person, not a log parser.
    """

    ok: bool
    path: Path | None = None
    size_bytes: int = 0
    error: str = ""

    @property
    def filename(self) -> str:
        """The captured file's name, or ``""`` when nothing was written."""
        return self.path.name if self.path is not None else ""


def resolve_screenshot_dir(configured: str | Path | None) -> Path:
    """Resolve the configured screenshot directory to an absolute path.

    Empty or missing config falls back to :data:`DEFAULT_SCREENSHOT_DIR`.  The
    resolution matches ``cache_dir`` and ``upload_dir``: relative paths are
    joined onto the persistent data directory.
    """
    raw = str(configured).strip() if configured else ""
    return resolve_install_path(raw or DEFAULT_SCREENSHOT_DIR)


def _unique_path(directory: Path, stamp: str) -> Path:
    """Return a free ``screenshot-<stamp>.png`` path inside *directory*.

    Two captures within the same second would otherwise overwrite each other.
    The timestamp stays readable, and the disambiguating suffix is only added
    when it is genuinely needed.
    """
    candidate = directory / f"screenshot-{stamp}.png"
    suffix = 2
    while candidate.exists():
        candidate = directory / f"screenshot-{stamp}-{suffix}.png"
        suffix += 1
    return candidate


def capture(destination: Path, *, now: float | None = None) -> CaptureResult:
    """Capture the composited frame into *destination* as a PNG.

    Args:
        destination: Directory to write into.  Created if missing, because a
            desktop session has no ``reconcile.sh`` to create it.
        now: Unix timestamp used for the filename.  Injected by tests so the
            resulting name is deterministic.
    """
    stamp = datetime.fromtimestamp(time.time() if now is None else now).strftime(_STAMP_FORMAT)

    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Cannot create screenshot dir %s: %s", destination, exc)
        return CaptureResult(ok=False, error=f"Cannot create {destination}: {exc}")

    path = _unique_path(destination, stamp)
    try:
        proc = subprocess.run(
            [_GRIM_BIN, str(path)],
            env=wayland_env(),
            capture_output=True,
            text=True,
            timeout=CAPTURE_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        # grim ships in requirements-system.txt, so a missing binary means this
        # device has not taken an update since it was added.
        logger.warning("grim not found at %s — is the grim package installed?", _GRIM_BIN)
        return CaptureResult(ok=False, error="grim is not installed on this device")
    except subprocess.TimeoutExpired:
        logger.warning("grim timed out after %.0fs", CAPTURE_TIMEOUT_S)
        return CaptureResult(ok=False, error="Screen capture timed out")
    except OSError as exc:
        logger.warning("grim could not be run: %s", exc)
        return CaptureResult(ok=False, error=str(exc))

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        logger.warning("grim failed (rc=%d): %s", proc.returncode, detail)
        return CaptureResult(ok=False, error=detail or f"grim exited with status {proc.returncode}")

    try:
        size = path.stat().st_size
    except OSError as exc:
        return CaptureResult(ok=False, error=f"grim wrote no file: {exc}")

    if size == 0:
        # grim can exit 0 having written nothing when the compositor refuses
        # the screencopy request.  An empty PNG shown in a file browser is a
        # worse outcome than a clear error, so remove it.
        path.unlink(missing_ok=True)
        return CaptureResult(ok=False, error="Screen capture produced an empty file")

    logger.info("Screenshot saved: %s (%d bytes)", path, size)
    return CaptureResult(ok=True, path=path, size_bytes=size)


def clear_screenshots(directory: Path) -> tuple[int, int]:
    """Delete every file directly inside *directory*.

    Returns ``(deleted, freed_bytes)``, mirroring ``clear_cache`` so the route
    can report the result the same way.  Subdirectories are left alone, and a
    directory that does not exist yet is not an error — nothing to clear is the
    correct answer, not a failure.
    """
    deleted = 0
    freed = 0
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return 0, 0
    except OSError as exc:
        logger.warning("Cannot list screenshot dir %s: %s", directory, exc)
        return 0, 0

    for entry in entries:
        if not entry.is_file():
            continue
        try:
            freed += entry.stat().st_size
            entry.unlink()
            deleted += 1
        except OSError as exc:
            logger.warning("Could not delete %s: %s", entry, exc)

    if deleted:
        logger.info("Cleared %d screenshot(s), freed %.1f MB", deleted, freed / (1024 * 1024))
    return deleted, freed
