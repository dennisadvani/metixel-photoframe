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
from metixel.shared import logging_setup
from metixel.shared.paths import data_dir

#: Sentinel level above CRITICAL (50): no log record can pass this filter, so
#: setting it on the file handler effectively disables on-disk logging.
#:
#: The canonical value and the whole name → level map live in
#: :mod:`metixel.shared.logging_setup`; this alias is kept as the name the CLI
#: and its tests use, so the *value* still has a single owner.
_LOG_LEVEL_NONE = logging_setup.NONE


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
    try:
        import json as _json

        if config_path.exists():
            raw = _json.loads(config_path.read_text(encoding="utf-8"))
            return logging_setup.parse_level(raw.get("system", {}).get("log_level", "NONE"))
    except Exception:
        # Unreadable/corrupt config: fall through to the safe default.
        pass
    return logging_setup.NONE


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

    Three levels are in play, and :mod:`metixel.shared.logging_setup` owns the
    relationship between them (read its docstring before changing anything
    here):

    * the **file** gets ``system.log_level`` exactly — the user's choice, and the
      only sink that costs SD-card writes;
    * the **live view** (the ring buffer behind the Logs card) always keeps INFO
      and above, and follows the setting down to DEBUG, so the card is never
      blank under the default ``NONE``;
    * the **logger** is opened up to the most permissive of those and no further,
      because a handler's level can only ever remove records, never add them.

    The levels are applied by :func:`~metixel.shared.logging_setup.apply_level`
    *after* every handler is attached — see the note at the call site.

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

    # A process with no file sink has no file level: NONE keeps it out of the
    # calculation below rather than silently adopting the user's file setting.
    file_level = logging_setup.NONE

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

    # 3. Ring buffer for the dashboard's live log view.
    #
    #    Attached to the ROOT logger ONLY.  Attaching the same handler to both
    #    root and "metixel" captured every metixel record twice: a record
    #    propagates up the hierarchy and `logging` does not de-duplicate a
    #    handler shared between an ancestor and a descendant, so the Logs card
    #    showed every Metixel line in duplicate.  Root alone sees everything —
    #    metixel's records by propagation, and third-party records (werkzeug,
    #    urllib3) directly.
    from metixel.shared.log_buffer import LogRingBuffer

    ring_buffer = LogRingBuffer(capacity=500)
    ring_buffer.setFormatter(fmt)
    root.addHandler(ring_buffer)

    # 4. Apply the levels LAST, once every handler exists.
    #
    #    Ordering is load-bearing.  Handlers decide *what gets written*, but the
    #    logger decides what is *created*, and a handler's level can only ever
    #    remove records — never add them.  Applying the file level before the
    #    ring buffer existed is why the buffer's level had to be hardcoded, and
    #    why selecting "Debug" produced no debug output anywhere.  One call,
    #    after the handlers are in place, keeps all three levels consistent.
    effective = logging_setup.apply_level(file_level, terminal_level=log_level)
    logging.getLogger("metixel").debug(
        "Logging configured: file=%s, live view=%s, %s logger=%s",
        logging.getLevelName(file_level),
        logging.getLevelName(logging_setup.live_view_level(file_level)),
        logging_setup.PACKAGE_LOGGER,
        logging.getLevelName(effective),
    )


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
