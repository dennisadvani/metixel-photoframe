# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Ambient backdrop blur — a throttled, one-shot subprocess.

The ``blur`` ambient fill is a full-screen, stretched copy of the artwork with a
box or gaussian blur over it (the TV-letterbox effect).  Building that inside the
frontend process is what made crossfades judder: the Pillow filter plus the
``QImage -> PNG -> PIL -> PNG -> QImage`` round trip holds the CPU for tens of
milliseconds, and a render loop that loses frames mid-crossfade looks broken.

So the work runs in a short-lived child process, throttled exactly like the
media pipeline's own workers:

* ``nice -n 19`` — the render loop always wins the CPU
* ``cpulimit -l 50`` — a hard 50 % ceiling, where ``cpulimit`` is installed

Three deliberate choices are worth knowing before changing anything here:

**The child is a CLI, not a thread.** ``nice`` and ``cpulimit`` are
process-level, so throttling requires a real process.  That also gets the
frontend out of the business of importing Pillow on its render path.

**The result is a file in tmpfs, not a pipe.** ``BackdropRunner`` writes to
``run_dir()/ambient/`` — RAM, discarded at reboot.  A full-screen image written
to the SD card on every slide boundary would be exactly the continuous-write
pattern the project forbids, for a file that is worthless once the frame moves
on.

**The child's stdout is never parsed.** ``cpulimit`` prints its own progress
lines, which once corrupted a worker's JSON result.  Success is signalled by
the exit code alone; everything the child prints is discarded.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from metixel.shared.paths import run_dir
from metixel.shared.throttle import throttle_cmd

logger = logging.getLogger(__name__)

#: Hard CPU ceiling for a blur, as a percentage of ONE core.  Half a core is
#: plenty for a job this short, and leaves the render loop and the web server
#: untouched even on a Pi 3.
WORKER_CPU_LIMIT = 50

#: How long a job may run before it is killed and reported as failed.  A blur is
#: milliseconds of work, so anything near this is a wedged child — and the
#: caller must be told rather than left waiting on a file that will never
#: appear.
DEFAULT_TIMEOUT_S = 30.0

#: JPEG quality for the stored backdrop.  It is blurred, then dimmed behind the
#: artwork, so a higher quality would be invisible; this is already generous.
_JPEG_QUALITY = 85

#: Blur radii are clamped to this range.  Below 1 there is nothing to blur, and
#: beyond 100 the artwork is unrecognisable while the filter cost keeps rising.
MIN_RADIUS = 1.0
MAX_RADIUS = 100.0


def clamp_radius(radius: float) -> float:
    """Constrain *radius* to the usable range, in screen pixels."""
    return max(MIN_RADIUS, min(MAX_RADIUS, float(radius)))


#: Blur kernels a backdrop can be built with, in the order the GUI offers them.
#: ``box`` is the cheap default; ``gaussian`` is smoother at ~2.4x the cost for
#: the same radius, which is why it is a choice rather than the only option.
FILTERS: tuple[str, ...] = ("box", "gaussian")

#: Kernel used when the configured value is missing or unrecognised.
DEFAULT_FILTER = "box"


def resolve_filter(value: object) -> str:
    """Coerce *value* to a supported kernel name, defaulting on nonsense.

    A hand-edited config — or one written by a release that predates this key —
    must not stop the slideshow, so an unusable value falls back exactly as an
    out-of-range radius does.  Never raises.
    """
    if isinstance(value, str) and value.strip().lower() in FILTERS:
        return value.strip().lower()
    if value not in (None, ""):
        logger.warning("Unknown ambient blur filter %r — using %r", value, DEFAULT_FILTER)
    return DEFAULT_FILTER


# ---------------------------------------------------------------------------
# The blur itself (runs in the child)
# ---------------------------------------------------------------------------


def blur_to_file(
    source: Path,
    dest: Path,
    width: int,
    height: int,
    radius: float,
    blur_filter: object = None,
) -> bool:
    """Stretch *source* to *width* x *height*, blur it, and save to *dest*.

    The aspect ratio is ignored on purpose: the backdrop has to cover every
    pixel, and the letterbox gap a ``contain`` fit leaves is the very thing the
    effect exists to fill.

    Two kernels are offered, because they trade cost against falloff:

    * ``box`` — a separable running-sum filter, and the default.  Measured at
      1920x1200 on a Pi 5: ``BoxBlur(24)`` 56 ms against ``GaussianBlur(24)``
      134 ms, with the difference invisible behind a dimmed backdrop.
    * ``gaussian`` — the smoother of the two, for photos whose soft gradients
      make the box kernel's square shoulders visible.

    Returns ``False`` on any failure — an unreadable source, a missing Pillow, a
    full disk — so the caller degrades to the flat fill instead of blanking the
    frame.  Never raises.
    """
    try:
        from PIL import Image, ImageFilter
    except ImportError:  # pragma: no cover - Pillow is a runtime dependency
        logger.warning("Pillow unavailable — cannot build an ambient backdrop")
        return False

    try:
        with Image.open(source) as opened:
            artwork = opened.convert("RGB")
        # ``resize`` with no aspect argument IS IgnoreAspectRatio.
        stretched = artwork.resize((max(1, int(width)), max(1, int(height))))
        pixels = clamp_radius(radius)
        if resolve_filter(blur_filter) == "gaussian":
            blurred = stretched.filter(ImageFilter.GaussianBlur(pixels))
        else:
            blurred = stretched.filter(ImageFilter.BoxBlur(pixels))
        dest.parent.mkdir(parents=True, exist_ok=True)
        blurred.save(dest, format="JPEG", quality=_JPEG_QUALITY)
        return True
    except Exception:
        logger.debug("Ambient blur failed for %s", source, exc_info=True)
        return False


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  Exit status is the only result channel."""
    parser = argparse.ArgumentParser(
        prog="metixel-ambient-blur",
        description="Build one blurred ambient backdrop from a media file.",
    )
    parser.add_argument("--source", required=True, type=Path, help="image to blur")
    parser.add_argument("--dest", required=True, type=Path, help="JPEG to write")
    parser.add_argument("--width", required=True, type=int, help="target width in pixels")
    parser.add_argument("--height", required=True, type=int, help="target height in pixels")
    parser.add_argument("--radius", required=True, type=float, help="blur radius in pixels")
    parser.add_argument(
        "--filter",
        choices=FILTERS,
        default=DEFAULT_FILTER,
        help="blur kernel (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    ok = blur_to_file(args.source, args.dest, args.width, args.height, args.radius, args.filter)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Job identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackdropRequest:
    """Everything a backdrop depends on, and therefore its identity.

    Value equality is the cache test: two requests are the same backdrop exactly
    when the file, its fingerprint, the target size, the radius and the kernel
    all match.  A screen-size, radius or kernel change therefore invalidates
    every outstanding backdrop with no explicit invalidation call, which is the
    only kind of invalidation that cannot be forgotten.

    The file's ``(mtime_ns, size)`` is part of it so that re-optimising a photo
    in place — which rewrites the same cache path — cannot leave a stale
    backdrop behind under an unchanged name.
    """

    source: Path
    mtime_ns: int
    size: int
    width: int
    height: int
    radius: float
    blur_filter: str = DEFAULT_FILTER

    @classmethod
    def build(
        cls,
        source: Path | None,
        width: int,
        height: int,
        radius: float,
        blur_filter: object = None,
    ) -> BackdropRequest | None:
        """Fingerprint *source*, or ``None`` when it cannot be read.

        ``None`` is the honest "there is nothing to build" answer, which callers
        treat as "show the flat fill and do not wait".
        """
        if source is None:
            return None
        try:
            stat = source.stat()
        except OSError:
            return None
        return cls(
            source=Path(source),
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            width=max(1, int(width)),
            height=max(1, int(height)),
            radius=round(clamp_radius(radius), 2),
            blur_filter=resolve_filter(blur_filter),
        )

    @property
    def job_id(self) -> str:
        """Short stable hash of the identity, used as the output filename."""
        raw = (
            f"{self.source}|{self.mtime_ns}|{self.size}"
            f"|{self.width}x{self.height}|{self.radius}|{self.blur_filter}"
        )
        return hashlib.sha256(raw.encode("utf-8", "surrogateescape")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# The runner (lives in the frontend process)
# ---------------------------------------------------------------------------


class BackdropRunner:
    """Runs one ambient-blur job at a time in a throttled subprocess.

    Deliberately a single slot rather than a pool: backdrops are prepared one
    slide ahead, so concurrency could only ever add CPU contention to the very
    render loop the throttling exists to protect.  A new request supersedes the
    job in flight, mirroring :class:`~metixel.frontend.presentation.image_cache.ImageCache`.

    Every method is safe to call from the GUI thread and none of them block:
    :meth:`start` spawns and returns, :meth:`take_finished` polls.
    """

    def __init__(
        self,
        tmp_dir: Path | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        cpu_limit: int | None = WORKER_CPU_LIMIT,
    ) -> None:
        self._tmp_dir = tmp_dir if tmp_dir is not None else run_dir() / "ambient"
        self._timeout_s = timeout_s
        self._cpu_limit = cpu_limit
        self._proc: subprocess.Popen[bytes] | None = None
        self._job_id: str | None = None
        self._dest: Path | None = None
        self._started_at: float = 0.0

    # -- Introspection -------------------------------------------------------

    @property
    def job_id(self) -> str | None:
        """The job in flight, or ``None`` when idle."""
        return self._job_id

    def output_path(self, job_id: str) -> Path:
        """Where the backdrop for *job_id* is written."""
        return self._tmp_dir / f"{job_id}.jpg"

    # -- Lifecycle -----------------------------------------------------------

    def start(self, request: BackdropRequest) -> None:
        """Begin building *request*.  Supersedes any job already running."""
        if self._job_id == request.job_id and self._running():
            return
        self.cancel()

        dest = self.output_path(request.job_id)
        cmd = throttle_cmd(
            [
                sys.executable,
                "-m",
                "metixel.display.ambient_blur",
                "--source",
                str(request.source),
                "--dest",
                str(dest),
                "--width",
                str(request.width),
                "--height",
                str(request.height),
                "--radius",
                str(request.radius),
                "--filter",
                request.blur_filter,
            ],
            self._cpu_limit,
        )
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # A new session, so superseding the job can kill the whole
                # group: terminating ``cpulimit`` alone would leave the Python
                # child it is babysitting running to completion.
                start_new_session=True,
            )
        except OSError:
            logger.debug("Could not start the ambient blur worker", exc_info=True)
            self._proc = None
            return

        self._job_id = request.job_id
        self._dest = dest
        self._started_at = time.monotonic()
        logger.debug(
            "Ambient blur started: %s -> %dx%d r=%.1f",
            request.source.name,
            request.width,
            request.height,
            request.radius,
        )

    def take_finished(self) -> tuple[str, Path | None] | None:
        """Report a job that ended since the last call, if any.

        Returns ``(job_id, path)`` on success, ``(job_id, None)`` on failure or
        timeout, and ``None`` while nothing has finished.  Never blocks and
        never raises.
        """
        job_id = self._job_id
        if self._proc is None or job_id is None:
            return None

        dest = self._dest
        code = self._proc.poll()
        if code is None:
            if time.monotonic() - self._started_at <= self._timeout_s:
                return None
            logger.warning(
                "Ambient blur timed out after %.0fs — falling back to the flat fill",
                self._timeout_s,
            )
            self.cancel()
            return job_id, None

        self._forget()
        if code != 0 or dest is None or not dest.is_file():
            logger.debug("Ambient blur worker exited with status %s", code)
            if dest is not None:
                dest.unlink(missing_ok=True)
            return job_id, None
        return job_id, dest

    def release(self, job_id: str) -> None:
        """Delete a finished backdrop once the caller has it in memory.

        Keeps tmpfs to the one file actually in flight; a rebuilt backdrop
        costs milliseconds and is rare, so there is nothing to be gained by
        keeping the bytes on disk.
        """
        self.output_path(job_id).unlink(missing_ok=True)

    def cancel(self) -> None:
        """Abandon the job in flight, if any.  Safe to call at any time."""
        proc, dest = self._proc, self._dest
        self._forget()
        if proc is not None and proc.poll() is None:
            _terminate_process_group(proc)
        if dest is not None:
            dest.unlink(missing_ok=True)

    def close(self) -> None:
        """Release the subprocess.  Called when the frontend shuts down."""
        self.cancel()

    # -- Internal ------------------------------------------------------------

    def _running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _forget(self) -> None:
        self._proc = None
        self._job_id = None
        self._dest = None


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    """Stop *proc* and anything it spawned, escalating to ``SIGKILL``.

    The child is its own session leader (see :meth:`BackdropRunner.start`), so a
    signal to the group reaches ``cpulimit`` and the Python process it runs.
    """
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    try:
        proc.wait(timeout=2.0)
        return
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=2.0)


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
