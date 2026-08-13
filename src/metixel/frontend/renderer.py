# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Frontend Renderer — main render loop orchestrator.

Runs the display backend, drives the presentation engine, renders widgets,
and watches for config changes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path

from metixel.display import detect_backend
from metixel.display.backend import DisplayBackend
from metixel.frontend.overlay import MessageLayer, OverlayManager
from metixel.frontend.presentation.engine import PresentationEngine
from metixel.shared.config import Config
from metixel.shared.ipc import ControlMessage, IPCServer
from metixel.shared.models import MediaItem, MediaType, TranscodeStatus
from metixel.shared.system_stats import format_gpu_stats, read_system_stats

logger = logging.getLogger(__name__)


def _show_feedback(overlay: OverlayManager | None, title: str, body: str, duration: float) -> None:
    """Show a brief feedback popup on the frame display."""
    if overlay is None:
        return
    msgs = overlay.get_layer("messages")
    if msgs is not None:
        msgs.show(title=title, body=body, severity="info", duration=duration)


class FrontendRenderer:
    """Main frontend process — owns the GPU context and render loop.

    Responsibilities:
    - Initialize the display backend (pi3d, wayland, or dev)
    - Run the render loop at a fixed tick rate
    - Drive the presentation engine (slideshow + transitions)
    - Render widget overlay layer
    - Poll for control messages via IPC socket
    - Watch for config file changes via inotify (or polling fallback)
    """

    def __init__(self, config_path: Path, backend: DisplayBackend | None = None) -> None:
        self._config_path = config_path.resolve()
        self._config: Config = Config.load(config_path)
        self._backend = backend  # injected DisplayDriver port (None → detect_backend() in run())
        self._presentation: PresentationEngine | None = None
        self._overlay: OverlayManager | None = None
        self._ipc_server = IPCServer()
        self._running = False
        self._frame_count: int = 0
        self._last_config_check: float = 0.0
        self._config_mtime: float = 0.0
        # Playlist hot-reload tracking
        self._playlist_path: Path = Path("/run/metixel/playlist.json")
        self._playlist_mtime: float = 0.0
        self._last_playlist_check: float = 0.0
        # FPS tracking
        self._fps_last_time: float = 0.0
        self._fps_frame_snapshot: int = 0

    # -- Main loop -----------------------------------------------------------

    # ══════════════════════════════════════════════════════════════════════
    #  Boot screen is handled by BootLayer (overlay system via pi3d).
    #  See metixel.frontend.overlay.boot_layer.  No pygame dependency.
    # ══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _load_backend_playlist() -> list[MediaItem]:
        """Load media items from the backend's playlist.json.

        The backend writes this file incrementally during its initial
        scan/processing phase.  Loading from it ensures the frontend
        uses properly processed/cached files rather than raw source files.

        Returns an empty list if the playlist file doesn't exist yet
        (backend hasn't started) or is empty.
        """
        playlist_path = Path("/run/metixel/playlist.json")
        try:
            if not playlist_path.exists():
                logger.debug("Backend playlist not yet available: %s", playlist_path)
                return []
            with open(playlist_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read backend playlist: %s", e)
            return []

        items: list[MediaItem] = []
        for entry in data:
            try:
                mt_str = entry.get("media_type", "image")
                media_type = MediaType.VIDEO if mt_str == "video" else MediaType.IMAGE

                original = Path(entry["original_path"])
                cached = Path(entry["cached_path"])
                thumb = Path(entry["thumbnail_path"]) if entry.get("thumbnail_path") else None
                first_frame = (
                    Path(entry["first_frame_path"]) if entry.get("first_frame_path") else None
                )
                last_frame = (
                    Path(entry["last_frame_path"]) if entry.get("last_frame_path") else None
                )

                # Guard: skip items whose cached file doesn't exist yet
                # (e.g. transcoding still in progress from a previous run).
                # PLAY_ORIGINAL items (cached == original) always pass.
                if cached != original and (not cached.is_file() or cached.stat().st_size < 1024):
                    logger.debug(
                        "Skipping playlist entry — cached file not ready: %s",
                        cached.name,
                    )
                    continue

                items.append(
                    MediaItem(
                        id=entry["id"],
                        original_path=original,
                        cached_path=cached,
                        media_type=media_type,
                        width=entry.get("width", 0),
                        height=entry.get("height", 0),
                        duration_seconds=entry.get("duration_seconds", 0.0),
                        thumbnail_path=thumb,
                        first_frame_path=first_frame,
                        last_frame_path=last_frame,
                        source=entry.get("source", "local"),
                        transcode_status=(
                            TranscodeStatus(entry["transcode_status"])
                            if entry.get("transcode_status")
                            else None
                        ),
                    )
                )
            except (KeyError, TypeError) as e:
                logger.debug("Skipping malformed playlist entry: %s", e)
                continue

        if items:
            logger.info(
                "Loaded %d items from backend playlist (%d bytes)",
                len(items),
                playlist_path.stat().st_size,
            )
        return items

    def run(self) -> None:
        """Initialize and start the main render loop. Blocks until shutdown."""
        logger.info(
            "\n" + "=" * 70 + "\n  METIXEL FRONTEND STARTING  |  pid=%d  |  %s\n" + "=" * 70,
            os.getpid(),
            time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        display_cfg = self._config.display

        # ── Initialize display backend immediately ───────────────────
        # pi3d auto-detects native resolution when width=0 in config.
        if self._backend is None:
            self._backend = detect_backend()
        self._backend.create(
            width=display_cfg["width"],
            height=display_cfg["height"],
            fullscreen=display_cfg.get("fullscreen", True),
            hide_cursor=display_cfg.get("hide_cursor", True),
            fps_limit=display_cfg.get("fps_limit", 30),
        )

        logger.info(
            "Display: config=%dx%d, backend=%dx%d, fullscreen=%s, fps_limit=%d",
            display_cfg["width"],
            display_cfg["height"],
            self._backend.width,
            self._backend.height,
            display_cfg.get("fullscreen", True),
            display_cfg.get("fps_limit", 30),
        )

        # Write detected resolution to a status file so the web UI
        # can display it in the Display Settings card.
        try:
            run_dir = Path(os.environ.get("METIXEL_RUN_DIR", "/run/metixel"))
            run_dir.mkdir(parents=True, exist_ok=True)
            info_path = run_dir / "display_info.json"
            info = {
                "width": int(self._backend.width),
                "height": int(self._backend.height),
                "backend": type(self._backend).__name__,
            }
            tmp_path = info_path.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                json.dump(info, f)
            os.replace(tmp_path, info_path)
            logger.debug("Display info written to %s", info_path)
        except Exception:
            logger.warning("Could not write display info file", exc_info=True)

        # Initialize subsystems
        self._presentation = PresentationEngine(self._config, self._backend)

        # Initialize overlay layer system.
        # BootLayer (z=0.0, closest) covers the screen until the first
        # slideshow items are ready, then fades out to reveal them.
        from metixel.frontend.overlay.boot_layer import BootLayer

        self._overlay = OverlayManager()
        self._boot_layer = BootLayer()
        self._overlay.add_layer(self._boot_layer)
        self._overlay.add_layer(MessageLayer())
        self._boot_was_active = True  # Track for first-slide timer reset
        logger.info("Overlay system initialized: %d layers", len(self._overlay._layers))

        # ── Show persistent messages from config ─────────────────────
        # These are duration=0 messages that stay on screen until
        # dismissed via the web UI or API.  Used for first-boot
        # instructions (Wi-Fi setup, etc.).
        self._show_persistent_messages()

        # ── Queue loading moved to _render_loop() ────────────────────
        # The boot screen must appear immediately — scanning large
        # media folders can take minutes (1000+ files).  The render
        # loop now shows the boot layer first, then loads the queue.

        # Start IPC server (best-effort — may fail on dev machines without /run)
        try:
            self._ipc_server.start()
        except OSError:
            logger.debug("IPC server unavailable (expected on dev/Win) — controls disabled")

        # Track config file mtime for hot reload
        self._config_mtime = self._get_config_mtime()

        # Apply persisted log level to this process's file handlers.
        # (The backend API can also change it at runtime — the periodic
        # _check_config_changed() will pick up the new value.)
        self._apply_file_log_level()

        # Reset playlist mtime so _check_playlist_changed() loads items
        # immediately instead of waiting for the 3-second polling interval.
        self._playlist_mtime = 0.0

        self._running = True
        logger.info("Frontend render loop starting")

        try:
            self._render_loop()
        except KeyboardInterrupt:
            logger.info("Render loop interrupted")
        except Exception:
            logger.exception("Fatal error in render loop")
        finally:
            self._shutdown()

    def _render_loop(self) -> None:
        """The main render loop — runs at the configured FPS.

        Frame timing is handled by the display backend's ``loop_running()``
        (e.g., pygame's ``clock.tick()``), so we don't double-sleep here.
        """
        self._fps_last_time = time.monotonic()

        # ── Show boot screen immediately ──────────────────────────────
        # Render the boot layer before anything else so the user sees
        # feedback right away.  Scanning 1000+ media files can take
        # over a minute — the scan runs in a background thread so the
        # boot spinner keeps animating during the wait.
        if self._overlay:
            self._overlay.update({"video_playing": False})
            self._overlay.draw(self._backend)
        self._backend.loop_running()

        # ── Start background queue loader ─────────────────────────────
        # The folder scan can take minutes on large libraries.  Run it
        # in a daemon thread so the render loop keeps spinning (boot
        # screen stays animated).  The main thread checks a shared flag
        # and swaps in the queue once loading is done.
        _queue_ready = threading.Event()
        _loaded_items: list[MediaItem] = []

        def _load_initial_queue() -> None:
            nonlocal _loaded_items
            items: list[MediaItem] = list(self._load_backend_playlist())

            if items:
                logger.info("Loaded %d items from backend playlist", len(items))
            else:
                logger.warning(
                    "Backend playlist is empty — slideshow will wait for "
                    "the optimisation queue to process media.  The boot "
                    "screen stays visible until items are ready."
                )
            _loaded_items = items
            _queue_ready.set()

        loader_thread = threading.Thread(
            target=_load_initial_queue,
            daemon=True,
            name="queue-loader",
        )
        loader_thread.start()

        while self._running and self._backend and self._backend.loop_running():
            # ── Swap in initial queue when loading finishes ───────────
            if _queue_ready.is_set() and self._presentation._queue_loaded is False:
                self._presentation.set_queue(_loaded_items)
                # Signal the backend that the slideshow has started so
                # the network monitor can begin its AP-fallback countdown.
                # Only fire when there are actually items to display —
                # an empty initial load means the backend hasn't processed
                # any media yet; the hot-reload path will populate later.
                if _loaded_items:
                    self._notify_slideshow_started()
            # 1. Check for config changes (hot reload)
            self._check_config_changed()

            # 2. Process IPC control messages
            self._process_ipc()

            # 3. Render the current frame
            self._render_frame()

            # 4. Present to screen
            if self._backend:
                self._backend.swap_buffers()

            self._frame_count += 1

            # Log FPS every 5 seconds
            self._log_fps()

            # Log system resources every 30 seconds (debug builds only)
            self._log_resources()

    def _log_fps(self) -> None:
        """Log the actual frames-per-second every 5 seconds."""
        now = time.monotonic()
        elapsed = now - self._fps_last_time
        if elapsed >= 5.0:
            frames = self._frame_count - self._fps_frame_snapshot
            fps = frames / elapsed
            logger.debug(
                "FPS: %.1f (target=%d, frames=%d, elapsed=%.1fs)",
                fps,
                self._config.display.get("fps_limit", 30),
                frames,
                elapsed,
            )
            self._fps_last_time = now
            self._fps_frame_snapshot = self._frame_count

    # Track last resource log time (class-level to survive across frames)
    _last_resource_log: float = 0.0

    # Prevent duplicate slideshow-started notifications
    _slideshow_started_notified: bool = False

    def _notify_slideshow_started(self) -> None:
        """Notify the backend that the slideshow has begun playing.

        Sends a single HTTP POST to the backend's web server.  The
        backend network monitor defers its AP-fallback countdown until
        this signal arrives, avoiding CPU / I/O contention during the
        initial media processing phase.

        Only sends once — subsequent calls are silently ignored.
        Failures (e.g. backend not yet listening) are logged but not
        retried; the network monitor has a 5-minute hard timeout as a
        safety net.
        """
        if FrontendRenderer._slideshow_started_notified:
            return
        FrontendRenderer._slideshow_started_notified = True
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8080/api/slideshow-started",
                method="POST",
                data=b"",
            )
            urllib.request.urlopen(req, timeout=5)
            logger.info("Slideshow started notification sent to backend")
        except Exception:
            # Backend may not be ready yet — the network monitor's
            # 5-minute safety timeout will unblock it regardless.
            logger.debug("Failed to notify backend of slideshow start (backend may not be ready)")

    def _log_resources(self) -> None:
        """Log CPU, memory, swap, and GPU usage every 30 seconds (DEBUG only).

        Reads from ``/proc/stat``, ``/proc/meminfo``, and ``/proc/loadavg``.
        Non-Linux systems (dev/Win) are silently skipped.
        """
        now = time.monotonic()
        if now - FrontendRenderer._last_resource_log < 30.0:
            return
        FrontendRenderer._last_resource_log = now

        stats = read_system_stats()
        if stats is None:
            return  # Non-Linux or /proc not available
        load = stats["loadavg"]
        logger.debug(
            "RES: CPU=%.1f%%  MEM=%d/%dMB (%.1f%%)  SWAP=%d/%dMB  LOAD=%s %s %s",
            stats["cpu_percent"],
            stats["mem_used_mb"],
            stats["mem_total_mb"],
            stats["mem_percent"],
            stats["swap_used_mb"],
            stats["swap_total_mb"],
            load[0],
            load[1],
            load[2],
        )

        # ── GPU memory (Pi only) ─────────────────────────────────────
        if self._backend:
            gpu = self._backend.gpu_memory_info()
            if gpu and "gpu_total_mb" in gpu:
                logger.debug("GPU: %s", format_gpu_stats(gpu))

    def _render_frame(self) -> None:
        """Render a single frame."""
        if not self._backend or not self._presentation:
            return

        # Render presentation (slideshow + transition + matte)
        self._presentation.render()

        # Render overlay layers on top of slideshow.
        # Clear depth first so slideshow depth values don't occlude overlay.
        self._backend.clear_depth()
        if self._overlay:
            # Pass video state so message timers pause during VLC playback
            video_playing = (
                self._presentation._video_state != 0  # _VIDEO_IDLE
                if hasattr(self._presentation, "_video_state")
                else False
            )
            # Tell the boot layer whether the frontend has actually
            # loaded its queue — not just whether the backend wrote
            # playlist.json to disk.  This prevents the boot screen
            # from fading to a black screen when queue loading is
            # deferred to a background thread.
            #
            # Additionally, the active GPU texture slot must be
            # non-None before we signal readiness.  If the first
            # slide's texture hasn't been uploaded yet (e.g. a sync
            # load is still in progress), the render loop stalls
            # and the fade timer would expire before any frames
            # are drawn — producing a jump cut instead of a smooth
            # crossfade.  Waiting for the texture ensures the fade
            # runs over actual rendered frames.
            slideshow_ready = (
                self._presentation._queue_loaded
                and len(self._presentation._queue) > 0
                and self._presentation._tex[self._presentation._active] is not None
            )
            queue_size = len(self._presentation._queue) if self._presentation else 0
            self._overlay.update(
                {
                    "video_playing": video_playing,
                    "slideshow_ready": slideshow_ready,
                    "queue_size": queue_size,
                }
            )
            self._overlay.draw(self._backend)

        # ── Boot → slideshow transition ───────────────────────────────
        # When the boot layer finishes fading, reset the first slide's
        # display timer so it gets its full configured duration.
        if (
            getattr(self, "_boot_layer", None) is not None
            and self._boot_layer.is_done
            and getattr(self, "_boot_was_active", False)
        ):
            self._boot_was_active = False
            if self._presentation:
                self._presentation.reset_slide_timer()
                logger.info("First slide timer reset — boot screen finished")

    # -- Hot reload ----------------------------------------------------------

    def _check_config_changed(self) -> None:
        """Check if the config file or playlist has been modified on disk.

        Uses file mtime comparison (works cross-platform, no inotify dependency).
        On Linux with inotify, we could use a more efficient approach.
        """
        now = time.monotonic()
        # Only check every 500ms to avoid disk thrashing
        if now - self._last_config_check < 0.5:
            return
        self._last_config_check = now

        # -- Config hot reload --
        new_mtime = self._get_config_mtime()
        if new_mtime > self._config_mtime:
            logger.info("Config file changed — hot reloading")
            old_log_level = self._config.system.get("log_level", "NONE")
            self._config = Config.load(self._config_path)
            self._config_mtime = new_mtime
            # Re-initialize components that depend on config
            if self._presentation:
                self._presentation.reload_config(self._config)
            # Re-apply file log level if it changed (matches backend behaviour)
            new_log_level = self._config.system.get("log_level", "NONE")
            if new_log_level != old_log_level:
                self._apply_file_log_level()

        # -- Playlist hot reload (backend may add items from Immich sync) --
        # Poll every 0.5s when the queue is empty (boot phase) so the
        # first items are loaded quickly; throttle to 3s during normal
        # operation to reduce disk I/O.
        queue_empty = self._presentation is not None and not self._presentation._queue
        poll_interval = 0.5 if queue_empty else 3.0
        if now - self._last_playlist_check >= poll_interval:
            self._last_playlist_check = now
            self._check_playlist_changed()

    def _check_playlist_changed(self) -> None:
        """Reload the slideshow queue if the backend has updated the playlist.

        Detects both additions and removals by comparing the backend
        playlist IDs with the frontend's in-memory queue:

        - Items in the backend playlist but NOT in the frontend queue
          are added via ``add_items()`` (preserves current position).
        - Items in the frontend queue but NOT in the backend playlist
          are removed via ``remove_items()`` (e.g. deleted files or
          disabled watch folders).
        - If the playlist is empty, the entire queue is reset.

        Uses ``set_queue()`` for the initial load and ``add_items()`` /
        ``remove_items()`` for incremental updates to avoid restarting
        the slideshow from the beginning on every Immich sync batch.
        """
        try:
            new_mtime = os.path.getmtime(self._playlist_path)
        except OSError:
            return  # Playlist file doesn't exist yet

        if new_mtime <= self._playlist_mtime:
            return  # No change

        self._playlist_mtime = new_mtime
        logger.info("Playlist updated by backend — loading new items")

        try:
            with open(self._playlist_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            logger.warning("Failed to read playlist file — will retry")
            return

        items: list[MediaItem] = []
        for entry in data:
            try:
                item = MediaItem(
                    id=entry["id"],
                    original_path=Path(entry["original_path"]),
                    cached_path=Path(entry["cached_path"]),
                    media_type=MediaType(entry["media_type"]),
                    width=entry.get("width", 0),
                    height=entry.get("height", 0),
                    duration_seconds=entry.get("duration_seconds", 0.0),
                    thumbnail_path=Path(entry["thumbnail_path"])
                    if entry.get("thumbnail_path")
                    else None,
                    first_frame_path=Path(entry["first_frame_path"])
                    if entry.get("first_frame_path")
                    else None,
                    last_frame_path=Path(entry["last_frame_path"])
                    if entry.get("last_frame_path")
                    else None,
                    source=entry.get("source", "local"),
                    transcode_status=(
                        TranscodeStatus(entry["transcode_status"])
                        if entry.get("transcode_status")
                        else None
                    ),
                )
                items.append(item)
            except (KeyError, ValueError) as e:
                logger.debug("Skipping malformed playlist entry: %s", e)

        if not self._presentation:
            return

        if not items:
            # Playlist is empty — typically after a pipeline reset or
            # cache clear.  Reset the slideshow queue and re-show the
            # boot screen so the user sees the spinner instead of a
            # black screen while the pipeline rebuilds.
            logger.info("Backend playlist is empty — resetting slideshow queue")
            self._presentation.set_queue([])
            if getattr(self, "_boot_layer", None) is not None:
                self._boot_layer.reactivate()
                self._boot_was_active = True
            return

        if not self._presentation._queue:
            # Queue was empty (e.g. after reset above, or cold start).
            # Populate from scratch.
            self._presentation.set_queue(items)
            logger.info("Initialised slideshow queue with %d items", len(items))
            # If the initial background load found 0 items and this
            # hot-reload is the first time the queue gets populated,
            # notify the backend so the network monitor can unblock.
            self._notify_slideshow_started()
            return

        # ── Incremental diff: add new, update existing, remove stale ──
        backend_items = {item.id: item for item in items}
        frontend_ids = {item.id for item in self._presentation._queue}

        new_ids = set(backend_items.keys()) - frontend_ids
        removed_ids = frontend_ids - set(backend_items.keys())

        # Update metadata for items that exist in both — the backend
        # may have re-processed them (new dimensions, new frame paths
        # after re-transcode, etc.).  Without this, stale width/height/
        # first_frame_path/last_frame_path cause aspect ratio mismatches
        # and black frames during video transitions.
        updated = 0
        for item in self._presentation._queue:
            if item.id in backend_items:
                src = backend_items[item.id]
                if src.width > 0 and src.width != item.width:
                    item.width = src.width
                    updated += 1
                if src.height > 0 and src.height != item.height:
                    item.height = src.height
                    updated += 1
                if (
                    src.first_frame_path is not None
                    and src.first_frame_path != item.first_frame_path
                ):
                    item.first_frame_path = src.first_frame_path
                    updated += 1
                if src.last_frame_path is not None and src.last_frame_path != item.last_frame_path:
                    item.last_frame_path = src.last_frame_path
                    updated += 1
                if src.cached_path != item.cached_path:
                    item.cached_path = src.cached_path
                    updated += 1
        if updated:
            logger.info("Updated metadata for %d existing item(s)", updated)

        if new_ids:
            new_items = [item for item in items if item.id in new_ids]
            added = self._presentation.add_items(new_items)
            if added:
                logger.info(
                    "Added %d new items to slideshow (backend playlist: %d, frontend queue: %d)",
                    added,
                    len(items),
                    len(self._presentation._queue),
                )

        if removed_ids:
            # Only prune when the backend playlist has at least as many
            # items as the frontend queue.  If the backend playlist is
            # smaller, the backend is likely still building its initial
            # playlist (e.g. after a cache clear).  Removing items
            # prematurely would leave the slideshow with an empty queue
            # and cause a black screen.
            if len(items) >= len(self._presentation._queue):
                removed = self._presentation.remove_items(removed_ids)
                if removed:
                    logger.info(
                        "Removed %d items from slideshow "
                        "(backend playlist: %d, frontend queue: %d)",
                        removed,
                        len(items),
                        len(self._presentation._queue),
                    )
            else:
                logger.debug(
                    "Not pruning %d item(s) — backend playlist is still "
                    "building (%d items vs frontend %d).  Items will be "
                    "reconciled when the backend finishes processing.",
                    len(removed_ids),
                    len(items),
                    len(self._presentation._queue),
                )

        if not new_ids and not removed_ids:
            logger.debug(
                "Playlist mtime changed but no items added or removed (backend: %d, frontend: %d)",
                len(items),
                len(self._presentation._queue),
            )

    def _get_config_mtime(self) -> float:
        """Get the modification time of the config file."""
        try:
            return os.path.getmtime(self._config_path)
        except OSError:
            return 0.0

    def _apply_file_log_level(self) -> None:
        """Apply the persisted log level to all FileHandlers in this process.

        This runs at frontend startup and whenever the config changes,
        ensuring the frontend's file handlers stay in sync with what
        the user selected in the web UI (which only directly updates
        the backend process).
        """
        level_name = self._config.system.get("log_level", "NONE").upper()
        file_levels = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
            "NONE": 100,
        }
        target_level = file_levels.get(level_name, 100)

        updated = 0
        for logger_obj in logging.Logger.manager.loggerDict.values():
            if not isinstance(logger_obj, logging.Logger):
                continue
            for handler in logger_obj.handlers:
                if isinstance(handler, logging.FileHandler):
                    handler.setLevel(target_level)
                    updated += 1
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.FileHandler):
                handler.setLevel(target_level)
                updated += 1

        if updated:
            logger.debug(
                "Frontend file log level set to %s (%d handler(s) updated)",
                level_name,
                updated,
            )

    # -- IPC -----------------------------------------------------------------

    def _process_ipc(self) -> None:
        """Process any pending IPC control messages."""
        msg = self._ipc_server.poll()
        if msg:
            logger.debug("IPC command: %s", msg.cmd)
            self._handle_control_message(msg)

    def _handle_control_message(self, msg: ControlMessage) -> None:
        """Handle a control message from the backend."""
        if not self._presentation:
            return

        if msg.cmd == "next":
            self._presentation.next_item()
        elif msg.cmd == "prev":
            self._presentation.prev_item()
        elif msg.cmd == "pause":
            self._presentation.pause()
            _show_feedback(self._overlay, "Paused", "Slideshow paused", 3.0)
        elif msg.cmd == "resume":
            self._presentation.resume()
            _show_feedback(self._overlay, "Resumed", "Slideshow resumed", 3.0)
        elif msg.cmd == "toggle_pause":
            if self._presentation._paused:
                self._presentation.resume()
                _show_feedback(self._overlay, "Resumed", "Slideshow resumed", 3.0)
            else:
                self._presentation.pause()
                _show_feedback(self._overlay, "Paused", "Slideshow paused", 3.0)
        elif msg.cmd == "screen_off":
            if self._backend:
                self._backend.display_power(False)
        elif msg.cmd == "screen_on":
            if self._backend:
                self._backend.display_power(True)
        elif msg.cmd == "switch_album":
            album_id = msg.args.get("album_id", "")
            self._presentation.switch_album(album_id)
        elif msg.cmd == "show_message":
            if self._overlay:
                msgs = self._overlay.get_layer("messages")
                if msgs is not None:
                    msgs.show(
                        title=msg.args.get("title", ""),
                        body=msg.args.get("body", ""),
                        severity=msg.args.get("severity", "info"),
                        duration=float(msg.args.get("duration", 5.0)),
                    )
        elif msg.cmd == "dismiss_message":
            if self._overlay:
                msgs = self._overlay.get_layer("messages")
                if msgs is not None:
                    msgs.dismiss(msg.args.get("message_id", ""))
        elif msg.cmd == "dismiss_all_messages":
            if self._overlay:
                msgs = self._overlay.get_layer("messages")
                if msgs is not None:
                    msgs.dismiss_all()
        else:
            logger.warning("Unknown IPC command: %s", msg.cmd)

    def _show_persistent_messages(self) -> None:
        """Show config-defined persistent messages on the overlay.

        Persistent messages have ``duration=0`` (never auto-dismiss).
        They stay on screen until cleared via the dismiss API or the
        web dashboard.
        """
        messages_cfg = self._config.messages
        if not messages_cfg.get("enabled", True):
            return
        persistent = messages_cfg.get("persistent", [])
        if not persistent:
            return
        msgs = self._overlay.get_layer("messages") if self._overlay else None
        if msgs is None:
            return
        for entry in persistent:
            msgs.show(
                title=entry.get("title", ""),
                body=entry.get("body", ""),
                severity=entry.get("severity", "info"),
                duration=0,  # persistent — never auto-dismiss
                icon=entry.get("icon", ""),
            )
        logger.info("Showed %d persistent message(s) on boot", len(persistent))

    # -- Shutdown ------------------------------------------------------------

    def _shutdown(self) -> None:
        """Clean up all resources."""
        self._running = False
        logger.info(
            "=" * 70 + "\n  METIXEL FRONTEND STOPPING  |  pid=%d  |  %s\n" + "=" * 70,
            os.getpid(),
            time.strftime("%Y-%m-%d %H:%M:%S"),
        )

        if self._ipc_server:
            self._ipc_server.stop()

        if self._backend:
            self._backend.destroy()
            self._backend = None

        logger.info("Frontend shutdown complete")


def build_renderer(
    config_path: Path,
    backend: DisplayBackend | None = None,
) -> FrontendRenderer:
    """Composition root for the frontend process.

    Constructs :class:`FrontendRenderer` with the display backend selected
    by :func:`metixel.display.detect_backend` (or an injected backend for
    tests and alternate deployments).
    """
    return FrontendRenderer(config_path=config_path, backend=backend)
