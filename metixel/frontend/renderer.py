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
import time
from pathlib import Path

from metixel.display import detect_backend
from metixel.display.backend import DisplayBackend
from metixel.frontend.overlay import OverlayManager, MessageLayer
from metixel.frontend.presentation.engine import PresentationEngine
from metixel.shared.config import Config
from metixel.shared.ipc import ControlMessage, IPCServer
from metixel.shared.models import MediaItem, MediaType, TranscodeStatus

logger = logging.getLogger(__name__)


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

    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path.resolve()
        self._config: Config = Config.load(config_path)
        self._backend: DisplayBackend | None = None
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

    @staticmethod
    def _hide_and_warp_cursor(display_w: int, display_h: int) -> None:
        """Hide the mouse cursor and warp it to the bottom-right corner.

        Called once during display initialization.  pi3d's
        ``DISPLAY_CONFIG_HIDE_CURSOR`` hides the cursor within the pi3d
        window, but on some setups (cage/XWayland) the SDL2 cursor may
        briefly appear before pi3d finishes its first draw.  Warping to
        the corner ensures it's never visible.

        Uses the same approach as picframe:
        ``sdl2.SDL_WarpMouseInWindow()`` → bottom-right pixel.
        """
        try:
            import sdl2  # type: ignore
            # Disable the cursor sprite
            sdl2.SDL_ShowCursor(sdl2.SDL_DISABLE)
            # Warp to the bottom-right corner so even if the compositor
            # briefly shows a hardware cursor, it's off the visible area
            # (or at the very edge where it's least noticeable).
            if display_w > 1 and display_h > 1:
                # SDL_WarpMouseGlobal doesn't exist — use the pi3d/SDL2
                # focus window.  The pi3d display is the only SDL2 window
                # so GetMouseFocus() will find it after Display.create().
                focus = sdl2.SDL_GetMouseFocus()
                if focus:
                    sdl2.SDL_WarpMouseInWindow(focus, display_w - 1, display_h - 1)
                else:
                    # Window not yet available — try again with a short
                    # poll loop (the pi3d window may take a frame to appear).
                    for _ in range(50):
                        sdl2.SDL_PumpEvents()
                        focus = sdl2.SDL_GetMouseFocus()
                        if focus:
                            sdl2.SDL_WarpMouseInWindow(
                                focus, display_w - 1, display_h - 1,
                            )
                            break
                        time.sleep(0.01)
        except ImportError:
            pass  # pysdl2 not installed — cursor will be hidden by pi3d
        except Exception:
            logger.debug("Mouse warp failed (non-fatal)", exc_info=True)

    # -- Backend processing progress -----------------------------------------

    # Path to the progress file the backend's folder watcher writes.
    _PROCESSING_STATUS_PATH = "/run/metixel/processing_status.json"

    # How long to wait for the backend to start writing progress before
    # giving up and starting the slideshow anyway (seconds).
    # Increased from 60s → 300s: the backend writes playlist items
    # incrementally now, so the frontend will have images to show even
    # while processing is still ongoing.  Only abort if the backend
    # hasn't started at all (crash / failed launch).
    _PROCESSING_TIMEOUT = 300.0

    # Minimum time the progress screen stays visible so the user actually
    # sees it — even when all files are already cached from a previous run.
    _PROGRESS_MIN_DISPLAY = 2.0

    # Minimum number of items in the backend playlist before the splash
    # exits early.  Matches the backend's incremental flush batch size
    # so the frontend can start showing images as soon as the first batch
    # lands, without waiting for all 300+ files to finish processing.
    _PLAYLIST_READY_MIN = 12

    def _wait_for_backend_processing(self) -> tuple[int, int]:
        """Show a pygame boot splash while the backend processes media.

        Returns the detected display resolution ``(width, height)`` so
        the pi3d backend can be created with the correct dimensions.

        Pygame handles text, images and rectangles reliably — unlike
        pi3d's ``FixedString`` (GPU memory leak) and ``draw_rect``
        (inconsistent inside the pi3d render loop for splash use).

        After the splash is done, pygame is fully shut down so pi3d can
        claim the display without SDL2 conflicts.
        """
        # ── Hide cursor BEFORE any graphics init ────────────────────────
        # 1. X11-level: xsetroot (hides the hardware cursor on the X server)
        try:
            import subprocess
            subprocess.run(
                ["xsetroot", "-cursor", "/dev/null", "/dev/null"],
                timeout=2, capture_output=True,
            )
        except Exception:
            pass
        # 2. SDL2-level: disable the SDL cursor before pygame touches SDL
        try:
            import sdl2
            sdl2.SDL_ShowCursor(sdl2.SDL_DISABLE)
        except Exception:
            pass

        try:
            import pygame
        except ImportError:
            logger.warning("Pygame not available — skipping boot splash")
            return (0, 0)

        # ── Init pygame ─────────────────────────────────────────────────
        pygame.display.init()
        pygame.font.init()
        # 3. Pygame-level: double-insurance after display init
        pygame.mouse.set_visible(False)

        # Detect native resolution via fullscreen
        try:
            pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.NOFRAME)
        except pygame.error:
            pygame.display.set_mode((1920, 1080))
        surface = pygame.display.get_surface()
        if surface is None:
            pygame.quit()
            return (0, 0)
        display_w, display_h = surface.get_size()

        # When no monitor is connected, pygame/SDL2 may report 1×1 (or 0×0).
        # Fall back to a safe 1080p default so the splash screen is visible.
        if display_w <= 1 or display_h <= 1:
            logger.warning(
                "Boot splash detected %dx%d — likely no monitor connected. "
                "Falling back to 1920x1080.",
                display_w, display_h,
            )
            display_w, display_h = 1920, 1080
            try:
                pygame.display.set_mode(
                    (display_w, display_h),
                    pygame.FULLSCREEN | pygame.NOFRAME,
                )
            except pygame.error:
                pygame.display.set_mode((display_w, display_h))
            surface = pygame.display.get_surface()
            if surface is None:
                pygame.quit()
                return (0, 0)

        logger.info("Boot splash: %dx%d (pygame)", display_w, display_h)

        # 4. Once more after the window exists
        pygame.mouse.set_visible(False)

        # ── Colours ─────────────────────────────────────────────────────
        BAR_BORDER = (72, 72, 90)
        BAR_BG = (16, 16, 24)
        BAR_FILL = (160, 40, 40)   # dark red
        TEXT_COLOR = (220, 220, 230)
        TEXT_SUB = (180, 180, 195)

        # ── Load background image ───────────────────────────────────────
        bg_surf = None
        bg_path = Path(__file__).resolve().parent.parent / "assets" / "metitoobebebe3.png"
        if bg_path.is_file():
            try:
                raw = pygame.image.load(str(bg_path))
                bg_surf = pygame.transform.smoothscale(raw, (display_w, display_h))
            except Exception:
                logger.debug("Failed to load background image", exc_info=True)

        # ── Load logo ───────────────────────────────────────────────────
        logo_surf = None
        logo_rect = None
        logo_path = Path(__file__).resolve().parent.parent / "assets" / "metixel.png"
        if logo_path.is_file():
            try:
                raw = pygame.image.load(str(logo_path))
                # Scale to ~35% of screen width, preserving aspect ratio
                max_w = int(display_w * 0.35)
                if raw.get_width() > max_w:
                    scale = max_w / raw.get_width()
                    new_h = int(raw.get_height() * scale)
                    raw = pygame.transform.smoothscale(raw, (max_w, new_h))
                logo_surf = raw.convert_alpha()
                logo_rect = logo_surf.get_rect(
                    centerx=display_w // 2,
                    centery=display_h // 2,
                )
            except Exception:
                logger.debug("Failed to load logo", exc_info=True)

        # ── Fonts ───────────────────────────────────────────────────────
        try:
            font_large = pygame.font.Font(None, max(int(display_h * 0.04), 28))
            font_small = pygame.font.Font(None, max(int(display_h * 0.026), 18))
        except Exception:
            font_large = pygame.font.Font(None, 28)
            font_small = pygame.font.Font(None, 18)

        # ── Network info ────────────────────────────────────────────────
        import socket
        try:
            _hostname = socket.gethostname()
        except Exception:
            _hostname = "unknown"
        try:
            # Open a UDP socket to a public address to discover the
            # LAN-facing interface IP (gethostbyname often returns 127.x).
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            try:
                s.connect(("8.8.8.8", 80))
                _ip = s.getsockname()[0]
            except OSError:
                _ip = "unknown"
            finally:
                s.close()
        except Exception:
            _ip = "unknown"
        _net_text = f"{_hostname}  |  {_ip}"

        # ── Progress bar geometry ───────────────────────────────────────
        bar_w = int(display_w * 0.45)
        bar_h = max(int(display_h * 0.032), 12)
        bar_x = (display_w - bar_w) // 2
        bar_y = int(display_h * 0.78)
        border = 2

        # ── Polling loop ────────────────────────────────────────────────
        started = time.monotonic()
        status_seen: str | None = None
        target_pct: float = 0.0
        display_pct: float = 0.0
        processing_done: bool = False
        done_at: float = 0.0  # timestamp when bar first hit 100 %
        clock = pygame.time.Clock()

        while True:
            # Pump events so the window doesn't appear frozen
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    pygame.quit()
                    return (display_w, display_h)
                if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    pygame.quit()
                    return (display_w, display_h)

            now = time.monotonic()
            elapsed = now - started

            # ── Read backend status ─────────────────────────────────
            status = self._read_processing_status()
            phase = status.get("phase", "") if status else ""
            total = max(status.get("total", 0), 1) if status else 1
            processed = status.get("processed", 0) if status else 0
            playlist_count = self._count_playlist_items()

            if phase == "complete":
                target_pct = 1.0
                processing_done = True
            elif playlist_count >= self._PLAYLIST_READY_MIN:
                # Enough images are ready — snap the bar to 100 % so the
                # transition feels intentional.  The "hold for 2 s" logic
                # below still runs, giving the bar time to animate smoothly.
                target_pct = 1.0
                processing_done = True
            elif status is not None:
                target_pct = min(processed / total, 1.0)

            # Animate display toward target
            if display_pct < target_pct:
                display_pct += (target_pct - display_pct) * 0.12
                if abs(display_pct - target_pct) < 0.002:
                    display_pct = target_pct

            # Log phase transitions
            if phase and phase != status_seen:
                status_seen = phase
                if phase == "complete":
                    logger.info(
                        "Backend processing complete (%d/%d files) — "
                        "starting slideshow", processed, total,
                    )
                else:
                    logger.info(
                        "Backend progress: %s — %d/%d files",
                        phase, processed, total,
                    )

            # ── Render frame ────────────────────────────────────────
            if bg_surf is not None:
                surface.blit(bg_surf, (0, 0))
            else:
                surface.fill((180, 180, 190))  # fallback light grey

            # Logo
            if logo_surf is not None and logo_rect is not None:
                surface.blit(logo_surf, logo_rect)

            # Progress bar — border
            pygame.draw.rect(
                surface, BAR_BORDER,
                (bar_x - border, bar_y - border,
                 bar_w + 2 * border, bar_h + 2 * border),
            )
            # Progress bar — track
            pygame.draw.rect(
                surface, BAR_BG,
                (bar_x, bar_y, bar_w, bar_h),
            )
            # Progress bar — fill
            if display_pct > 0.001:
                fill_w = max(int(bar_w * display_pct), 4)
                pygame.draw.rect(
                    surface, BAR_FILL,
                    (bar_x, bar_y, fill_w, bar_h),
                )

            # Percentage text
            pct_text = font_large.render(
                f"{int(display_pct * 100)}%", True, TEXT_COLOR,
            )
            pct_rect = pct_text.get_rect(
                centerx=display_w // 2,
                centery=bar_y - int(display_h * 0.04),
            )
            surface.blit(pct_text, pct_rect)

            # Status message
            if processing_done:
                msg = "Starting slideshow…"
            elif status is None:
                msg = "Starting Metixel Photoframe…"
            elif phase == "scanning":
                msg = "Scanning media folder…"
            else:
                msg = f"Processing media… ({processed}/{total})"
            msg_text = font_small.render(msg, True, TEXT_SUB)
            msg_rect = msg_text.get_rect(
                centerx=display_w // 2,
                centery=bar_y + bar_h + int(display_h * 0.045),
            )
            surface.blit(msg_text, msg_rect)

            # Network info (hostname + IP)
            net_text = font_small.render(_net_text, True, TEXT_SUB)
            net_rect = net_text.get_rect(
                centerx=display_w // 2,
                centery=bar_y + bar_h + int(display_h * 0.09),
            )
            surface.blit(net_text, net_rect)

            pygame.display.flip()

            # ── Exit conditions ─────────────────────────────────────
            # 1. Ready to start — bar is at 100 % (either all files
            #    processed, or enough items in the playlist).  Hold for
            #    _PROGRESS_MIN_DISPLAY so the 100 % animation is visible.
            if processing_done and display_pct >= 0.999:
                if done_at == 0.0:
                    done_at = now
                if now - done_at >= self._PROGRESS_MIN_DISPLAY:
                    if phase == "complete":
                        logger.info(
                            "Boot splash finished — all %d files processed", total,
                        )
                    else:
                        logger.info(
                            "Boot splash finished — %d items in playlist "
                            "(%d/%d files processed)",
                            playlist_count, processed, total,
                        )
                    break

            # 2. Backend never started → timeout fallback.
            if status is None and elapsed > self._PROCESSING_TIMEOUT:
                logger.warning(
                    "Backend processing did not start within %.0fs — "
                    "starting slideshow anyway", self._PROCESSING_TIMEOUT,
                )
                break

            # 3. Absolute timeout safety net.
            if elapsed > self._PROCESSING_TIMEOUT:
                logger.warning(
                    "Backend processing timed out — "
                    "starting slideshow anyway (%d/%d processed)",
                    processed, total,
                )
                break

            clock.tick(30)

        # ── Clean shutdown of pygame ────────────────────────────────────
        pygame.display.quit()
        pygame.font.quit()
        pygame.quit()
        logger.info("Boot splash finished — handing over to pi3d")
        return (display_w, display_h)

    @staticmethod
    def _read_processing_status() -> dict | None:
        """Read the backend's processing status file. Returns None if not found."""
        try:
            with open(FrontendRenderer._PROCESSING_STATUS_PATH) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _count_playlist_items() -> int:
        """Count how many items the backend has written to playlist.json.

        Returns 0 if the file doesn't exist or is unreadable.  Used by
        the boot splash to decide when enough images are ready to start
        the slideshow without waiting for all processing to finish.
        """
        try:
            p = Path("/run/metixel/playlist.json")
            if not p.exists():
                return 0
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            return len(data) if isinstance(data, list) else 0
        except (json.JSONDecodeError, OSError):
            return 0

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

                # Guard: skip items whose cached file doesn't exist yet
                # (e.g. transcoding still in progress from a previous run).
                # PLAY_ORIGINAL items (cached == original) always pass.
                if cached != original:
                    if not cached.is_file() or cached.stat().st_size < 1024:
                        logger.debug(
                            "Skipping playlist entry — cached file not ready: %s",
                            cached.name,
                        )
                        continue

                items.append(MediaItem(
                    id=entry["id"],
                    original_path=original,
                    cached_path=cached,
                    media_type=media_type,
                    width=entry.get("width", 0),
                    height=entry.get("height", 0),
                    duration_seconds=entry.get("duration_seconds", 0.0),
                    thumbnail_path=thumb,
                    source=entry.get("source", "local"),
                    transcode_status=(
                        TranscodeStatus(entry["transcode_status"])
                        if entry.get("transcode_status")
                        else None
                    ),
                ))
            except (KeyError, TypeError) as e:
                logger.debug("Skipping malformed playlist entry: %s", e)
                continue

        if items:
            logger.info("Loaded %d items from backend playlist (%d bytes)",
                         len(items), playlist_path.stat().st_size)
        return items

    def run(self) -> None:
        """Initialize and start the main render loop. Blocks until shutdown."""
        logger.info(
            "\n" + "=" * 70 + "\n"
            "  METIXEL FRONTEND STARTING  |  pid=%d  |  %s\n"
            + "=" * 70,
            os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        display_cfg = self._config.display

        # ── Boot splash (pygame) — BEFORE pi3d claims the display ──────
        # Pygame is used for the boot screen because it handles text,
        # images and rectangles reliably.  It quits cleanly before pi3d
        # takes over, avoiding SDL2 conflicts.
        splash_w, splash_h = 0, 0
        if self._config.display.get("boot_splash", True):
            try:
                splash_w, splash_h = self._wait_for_backend_processing()
            except Exception:
                logger.exception("Progress screen failed — starting slideshow anyway")
        else:
            logger.info("Boot splash disabled by config — starting directly")

        # Initialize display backend (pi3d)
        self._backend = detect_backend()
        # Use splash-detected resolution if config is set to auto-detect.
        # Reject implausibly small resolutions (≤1 in either axis) —
        # these indicate no monitor is connected and should fall through
        # to the backend's own auto-detection + fallback logic.
        if (
            splash_w > 1 and splash_h > 1
            and display_cfg.get("width", 0) == 0
        ):
            display_cfg["width"] = splash_w
            display_cfg["height"] = splash_h
        self._backend.create(
            width=display_cfg["width"],
            height=display_cfg["height"],
            fullscreen=display_cfg.get("fullscreen", True),
            hide_cursor=display_cfg.get("hide_cursor", True),
            fps_limit=display_cfg.get("fps_limit", 30),
        )

        logger.info(
            "Display: config=%dx%d, backend=%dx%d, fullscreen=%s, fps_limit=%d",
            display_cfg["width"], display_cfg["height"],
            self._backend.width, self._backend.height,
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

        # -- Hide the mouse cursor ----------------------------------------
        # pi3d's DISPLAY_CONFIG_HIDE_CURSOR hides the cursor within the
        # pi3d window, but on some setups (cage/XWayland) the SDL2 cursor
        # may still be briefly visible.  Warp it to the bottom-right corner
        # so it's out of sight even before pi3d finishes initializing.
        self._hide_and_warp_cursor(
            int(self._backend.width), int(self._backend.height),
        )

        # Initialize subsystems
        self._presentation = PresentationEngine(self._config, self._backend)

        # Initialize overlay layer system
        self._overlay = OverlayManager()
        self._overlay.add_layer(MessageLayer())
        logger.info("Overlay system initialized: %d layers",
                     len(self._overlay._layers))

        # ── Load queue from the backend's playlist.json ───────────────
        # The backend writes playlist.json incrementally during processing.
        # Loading from it avoids the dual-scan problem: the frontend used
        # to scan folders directly, bypassing the backend's cached files
        # and starting with only a tiny subset of images.
        playlist_items = self._load_backend_playlist()

        # ── Fallback: scan folders directly if playlist is empty ──────
        # On first-ever boot or if the backend hasn't started yet, the
        # playlist file won't exist.  Fall back to direct folder scanning.
        if not playlist_items:
            logger.info("Backend playlist is empty — falling back to direct folder scan")
            from metixel.shared.config import resolve_watch_paths

            base_dir = self._config_path.parent.parent
            watch_paths = resolve_watch_paths(self._config, base_dir=base_dir)
            for folder in watch_paths:
                if folder.exists():
                    logger.info("Scanning media folder: %s", folder)
                    items = self._presentation.scan_folder(folder)
                    playlist_items.extend(items)
                    logger.info("Found %d items in %s", len(items), folder)
                else:
                    logger.debug("Watch path not found (skipping): %s", folder)

        if playlist_items:
            self._presentation.set_queue(playlist_items)
            logger.info(
                "Loaded %d items into slideshow queue",
                len(playlist_items),
            )
        else:
            logger.warning(
                "No images found — slideshow will show empty screen. "
                "Waiting for backend to process media…",
            )

        # Start IPC server (best-effort — may fail on dev machines without /run)
        try:
            self._ipc_server.start()
        except OSError:
            logger.debug("IPC server unavailable (expected on dev/Win) — controls disabled")

        # Track config file mtime for hot reload
        self._config_mtime = self._get_config_mtime()

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

        while self._running and self._backend and self._backend.loop_running():
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
            self._overlay.update()
            self._overlay.draw(self._backend)

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
            self._config = Config.load(self._config_path)
            self._config_mtime = new_mtime
            # Re-initialize components that depend on config
            if self._presentation:
                self._presentation.reload_config(self._config)

        # -- Playlist hot reload (backend may add items from Immich sync) --
        if now - self._last_playlist_check >= 3.0:  # Check every 3 seconds
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
                    thumbnail_path=Path(entry["thumbnail_path"]) if entry.get("thumbnail_path") else None,
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
            # Playlist is empty — typically after a cache clear.
            # Reset the queue completely so stale entries with dead
            # cache paths don't cause FileNotFoundError in the engine.
            logger.info("Backend playlist is empty — resetting slideshow queue")
            self._presentation.set_queue([])
            return

        if not self._presentation._queue:
            # Queue was empty (e.g. after reset above, or cold start).
            # Populate from scratch.
            self._presentation.set_queue(items)
            logger.info("Initialised slideshow queue with %d items", len(items))
            return

        # ── Incremental diff: add new, remove stale ──────────────────
        backend_ids = {item.id for item in items}
        frontend_ids = {item.id for item in self._presentation._queue}

        new_ids = backend_ids - frontend_ids
        removed_ids = frontend_ids - backend_ids

        if new_ids:
            new_items = [item for item in items if item.id in new_ids]
            added = self._presentation.add_items(new_items)
            if added:
                logger.info(
                    "Added %d new items to slideshow "
                    "(backend playlist: %d, frontend queue: %d)",
                    added, len(items), len(self._presentation._queue),
                )

        if removed_ids:
            removed = self._presentation.remove_items(removed_ids)
            if removed:
                logger.info(
                    "Removed %d items from slideshow "
                    "(backend playlist: %d, frontend queue: %d)",
                    removed, len(items), len(self._presentation._queue),
                )

        if not new_ids and not removed_ids:
            logger.debug(
                "Playlist mtime changed but no items added or removed "
                "(backend: %d, frontend: %d)",
                len(items), len(self._presentation._queue),
            )

    def _get_config_mtime(self) -> float:
        """Get the modification time of the config file."""
        try:
            return os.path.getmtime(self._config_path)
        except OSError:
            return 0.0

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
        elif msg.cmd == "resume":
            self._presentation.resume()
        elif msg.cmd == "power_off":
            if self._backend:
                self._backend.display_power(False)
        elif msg.cmd == "power_on":
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

    # -- Shutdown ------------------------------------------------------------

    def _shutdown(self) -> None:
        """Clean up all resources."""
        self._running = False
        logger.info(
            "=" * 70 + "\n"
            "  METIXEL FRONTEND STOPPING  |  pid=%d  |  %s\n"
            + "=" * 70,
            os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S"),
        )

        if self._ipc_server:
            self._ipc_server.stop()

        if self._backend:
            self._backend.destroy()
            self._backend = None

        logger.info("Frontend shutdown complete")
