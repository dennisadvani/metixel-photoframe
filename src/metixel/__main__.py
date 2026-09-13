# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""
Metixel Photoframe entry point.

Usage:
    python -m metixel --mode backend --config etc/config.json
    python -m metixel --mode frontend --config etc/config.json
"""

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from metixel import __version__
from metixel.shared.paths import data_dir

#: Sentinel level above CRITICAL (50): no log record can pass this filter, so
#: setting it on the file handler effectively disables on-disk logging.
_LOG_LEVEL_NONE = 100


def _log_file_for_mode(mode: str | None) -> Path:
    """Return the per-process log file for *mode*.

    Each process gets its OWN file.  They previously shared one path, so two
    independent ``RotatingFileHandler`` instances (backend + frontend) each kept
    their own byte counter and raced to rotate the same file — a rollover in
    one process truncated the other's output, which is why lines visible in the
    web UI never appeared in the log file.
    """
    name = {
        "backend": "metixel-backend.log",
        "frontend": "metixel-frontend.log",
    }.get(mode or "", "metixel.log")
    return data_dir() / "logs" / name


def _read_persisted_log_level(config_path: Path) -> int:
    """Return the file-handler level from ``system.log_level``.

    Read BEFORE any handler is created, because the level must be applied at
    handler-construction time.  A fresh device has NO ``config.json`` yet
    (``Config.load`` creates it later, in the daemon), so defaulting to ``NONE``
    here is the documented default — not a fallback for an error.
    """
    file_levels = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "NONE": _LOG_LEVEL_NONE,  # Above CRITICAL — disables disk logging
    }
    try:
        import json as _json

        if config_path.exists():
            raw = _json.loads(config_path.read_text(encoding="utf-8"))
            persisted = str(raw.get("system", {}).get("log_level", "NONE")).upper()
            return file_levels.get(persisted, _LOG_LEVEL_NONE)
    except Exception:
        # Unreadable/corrupt config: fall through to the safe default.
        pass
    return _LOG_LEVEL_NONE


def _setup_logging(
    config_path: Path,
    log_level: int,
    *,
    file_logging: bool = True,
    mode: str | None = None,
) -> None:
    """Set up logging: file + console + in-memory ring buffer.

    File logging is configured entirely in code: one file per process (see
    :func:`_log_file_for_mode`), with rotation decided here.  There is no
    user-editable logging config file.
    Also attaches a ``LogRingBuffer`` for the web UI.

    The **file handler** level is read from ``config.json`` →
    ``system.log_level`` so the user can control log file size
    via the web UI.  The ring buffer is always ``DEBUG`` so the
    dashboard severity checkboxes can filter the full stream.
    When ``file_logging`` is False (root-run entry points such as the
    cursor-hider daemon or the ``--clear-web-password`` one-shot) only the
    console and ring-buffer handlers are attached: the persistent on-disk
    ``metixel.log`` is never opened, so it stays owned by the pi user.
    """
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. Console handler (always)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(log_level)
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(log_level)
    root.addHandler(console)

    if file_logging:
        # File handler — path and rotation are decided in code, NOT read from a
        # user-editable logging.conf.  That file hardcoded a single shared log
        # path, so the backend and frontend each attached their own
        # RotatingFileHandler to the SAME file and truncated each other's
        # output; its handler level also overrode `system.log_level`, so
        # choosing INFO in the web UI still wrote DEBUG lines to disk.
        log_dir = data_dir() / "logs"
        log_file = _log_file_for_mode(mode)

        # Resolve the level FIRST and set it at construction time below.
        #
        # This ordering is load-bearing.  On a fresh device config.json does not
        # exist yet (Config.load creates it in the daemon, AFTER logging is
        # configured), so applying the level *after* adding the handler raced
        # that creation: handlers were built with DEBUG, then the level was
        # applied only if the file happened to exist already.  Result: a device
        # configured `log_level: NONE` still wrote a full log on its first run,
        # and the frontend (starting second, when the file did exist) honoured
        # NONE — so the two processes disagreed.
        file_level = _read_persisted_log_level(config_path)

        try:
            # On the Pi, scripts/reconcile.sh owns the data tree and has
            # already created data/logs.  This mkdir is a BEST-EFFORT
            # fallback for desktop/dev runs where no installer exists (and
            # it is harmless when the dir already exists: exist_ok=True).
            # It is not a second source of truth for the tree — reconcile.sh
            # owns the directory LIST and the ownership rules.
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                str(log_file),
                maxBytes=10_485_760,
                backupCount=5,
            )
            file_handler.setLevel(file_level)
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError as exc:
            # Log file unwritable (missing dir, bad ownership/perms, or a
            # read-only/full filesystem) → run console + ring buffer only.
            # Never crash the daemon over a log file (graceful degradation,
            # core rule 7) — this is the same failure mode as a root-owned
            # metixel.log crash-looping the pi backend.  The console handler
            # is attached above, so this warning is still visible at boot.
            logging.getLogger("metixel").warning(
                "%s not writable at %s; disabling file logging (%s)",
                log_file.name,
                log_file,
                exc,
            )

        # Re-apply across the hierarchy: modules that configured a logger before
        # this ran still need the level, and the web UI changes it at runtime
        # through the same helper, so startup and runtime cannot diverge.
        _apply_file_handler_levels(file_level)

    # 3. Ring buffer for web UI — attach to BOTH root and metixel loggers.
    #    The web API reads from the metixel logger's handlers, but non-
    #    metixel messages (werkzeug, urllib3, etc.) flow through root
    #    and should also be captured.
    #    Always DEBUG so the dashboard checkboxes can filter the full
    #    stream of log entries.
    from metixel.shared.log_buffer import LogRingBuffer

    ring_buffer = LogRingBuffer(capacity=500)
    ring_buffer.setLevel(logging.DEBUG)
    ring_buffer.setFormatter(fmt)
    root.addHandler(ring_buffer)

    # Attach the same buffer to the metixel logger so the web API finds it
    metixel_logger = logging.getLogger("metixel")
    metixel_logger.addHandler(ring_buffer)


def _apply_file_handler_levels(level: int) -> None:
    """Set every ``FileHandler`` across all loggers to *level*.

    Walks the logger hierarchy explicitly instead of iterating
    ``Logger.manager.loggerDict``.  That dict only holds *named* loggers that
    exist as direct values — handlers attached directly to a named logger such
    as ``metixel.backend.state`` were skipped, so those escaped the level set
    at startup and wrote DEBUG lines even when the user had selected INFO.  The
    root logger is included for completeness.

    Does **not** touch console handlers or ring buffers — only file-based
    handlers are affected.  This is the mechanism that lets the web UI control
    log file verbosity independently of the dashboard view.
    """
    seen: set[int] = set()

    def _apply(logger_obj: logging.Logger) -> None:
        if id(logger_obj) in seen:
            return
        seen.add(id(logger_obj))
        for handler in logger_obj.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.setLevel(level)

    # Every logger in the manager, plus the root, plus (defensively) the
    # metixel package logger and each already-instantiated metixel.* logger.
    for logger_obj in logging.Logger.manager.loggerDict.values():
        if isinstance(logger_obj, logging.Logger):
            _apply(logger_obj)
        elif isinstance(logger_obj, logging.PlaceHolder):
            continue
    _apply(logging.getLogger())
    _apply(logging.getLogger("metixel"))

    # Handlers can also be attached to a logger that was created lazily and is
    # therefore not yet in loggerDict at call time; walk the known metixel
    # namespaces to catch those.
    for name in list(logging.Logger.manager.loggerDict):
        if name == "metixel" or name.startswith("metixel."):
            _apply(logging.getLogger(name))


def _wants_file_logging(mode: str | None) -> bool:
    """Only the pi-run daemons (backend/frontend) write the persistent
    metixel.log.  Root-run entry points (cursor-hider, --clear-web-password)
    must not open it or the file ends up root-owned.
    """
    return mode in ("backend", "frontend")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Metixel Photoframe — Digital Photo Frame Application"
    )
    parser.add_argument(
        "--mode",
        choices=["backend", "frontend", "cursor-hider"],
        help=(
            "Run mode: backend (daemon + web), frontend (display renderer), "
            "or cursor-hider (hide the cage cursor via a virtual mouse)"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=data_dir() / "config.json",
        help="Path to configuration file (default: /opt/metixel/data/config.json)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--clear-web-password",
        action="store_true",
        help=(
            "Clear the optional web-dashboard password (auth disabled) and "
            "rotate the session-signing secret.  Recovery path for a forgotten "
            "password.  Does not start the daemon."
        ),
    )
    args = parser.parse_args()

    # NOTE: the persistent data tree is created and owned by
    # scripts/reconcile.sh, which runs as root on both the fresh-install and
    # OTA paths.  It is deliberately NOT created here: the app runs as pi and
    # cannot fix ownership of a directory left root-owned by an install, so a
    # second creator would only reintroduce the drift that crashed the backend
    # with PermissionError on /opt/metixel/data/logs.

    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    # Only the pi-run daemons (backend/frontend) write the persistent
    # metixel.log.  Root-run entry points (cursor-hider, --clear-web-password)
    # must never open it: a root-created metixel.log makes the pi backend
    # crash-loop with PermissionError (see metixel-backend.service ExecStartPre).
    file_logging = _wants_file_logging(args.mode)
    _setup_logging(args.config, log_level, file_logging=file_logging, mode=args.mode)

    logger = logging.getLogger("metixel")

    # Standalone admin action: clear the web password (forgot-password recovery).
    if args.clear_web_password:
        from metixel.backend.state import StateManager
        from metixel.backend.web.auth import WebAuthService

        state = StateManager(args.config)
        service = WebAuthService(state)
        service.clear_password()
        service.rotate_secret()
        logger.info("Web password cleared and auth secret rotated")
        print("Web password cleared. The dashboard no longer requires a login.")
        return

    if not args.mode:
        parser.error("--mode is required unless --clear-web-password is given")

    logger.info("Metixel Photoframe v%s starting in %s mode", __version__, args.mode)

    if args.mode == "backend":
        # Composition root: wire the real adapters and start the daemon.
        from metixel.backend.daemon import build_backend

        build_backend(config_path=args.config).run()
    elif args.mode == "frontend":
        # Composition root: select the display backend and start the renderer.
        from metixel.frontend.renderer import build_renderer

        build_renderer(config_path=args.config).run()
    elif args.mode == "cursor-hider":
        # Composition root: start the cursor-hiding daemon (runs as root).
        from metixel.display.cursor_hider import build_cursor_hider

        build_cursor_hider().run()


if __name__ == "__main__":
    main()
