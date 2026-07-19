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
import logging.config
import logging.handlers
import sys
from pathlib import Path

from metixel import __version__


def _setup_logging(config_path: Path, log_level: int) -> None:
    """Set up logging: file + console + in-memory ring buffer.

    Tries to load ``etc/logging.conf`` for file-based logging.
    Falls back to console-only if the config file is missing.
    Also attaches a ``LogRingBuffer`` for the web UI.

    The **file handler** level is read from ``config.json`` →
    ``system.log_level`` so the user can control log file size
    via the web UI.  The ring buffer is always ``DEBUG`` so the
    dashboard severity checkboxes can filter the full stream.
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

    # 2. File handler — use logging.conf if available, else default path
    log_conf = config_path.parent.parent / "etc" / "logging.conf"
    log_dir = Path("/opt/metixel/logs")
    log_file = log_dir / "metixel.log"

    if log_conf.exists():
        try:
            logging.config.fileConfig(str(log_conf), disable_existing_loggers=False)
        except Exception:
            pass  # Fall through to manual setup

    # Ensure file handler exists (may have been added by fileConfig, or add manually)
    has_file_handler = any(
        isinstance(h, logging.FileHandler) for h in root.handlers
    )
    if not has_file_handler:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            str(log_file), maxBytes=10_485_760, backupCount=5,
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    # 2b. Apply persisted file-handler log level from config.json.
    #     Only file handlers are changed — the ring buffer stays at
    #     DEBUG so the dashboard can always filter the full stream.
    try:
        import json as _json

        if config_path.exists():
            raw = _json.loads(config_path.read_text(encoding="utf-8"))
            persisted_level = raw.get("system", {}).get("log_level", "INFO").upper()
            file_levels = {
                "DEBUG": logging.DEBUG,
                "INFO": logging.INFO,
                "WARNING": logging.WARNING,
                "ERROR": logging.ERROR,
            }
            file_level = file_levels.get(persisted_level, logging.INFO)
            _apply_file_handler_levels(file_level)
    except Exception:
        pass  # Config file may not exist yet or be unreadable

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

    Does **not** touch console handlers or ring buffers — only
    file-based handlers are affected.  This is the mechanism that
    lets the web UI control log file verbosity independently of
    the dashboard view.
    """
    for logger_obj in logging.Logger.manager.loggerDict.values():
        if not isinstance(logger_obj, logging.Logger):
            continue
        for handler in logger_obj.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.setLevel(level)
    # Root logger
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            handler.setLevel(level)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Metixel Photoframe — Digital Photo Frame Application"
    )
    parser.add_argument(
        "--mode",
        choices=["backend", "frontend"],
        required=True,
        help="Run mode: backend (daemon + web) or frontend (display renderer)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("etc/config.json"),
        help="Path to configuration file (default: etc/config.json)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    _setup_logging(args.config, log_level)

    logger = logging.getLogger("metixel")
    logger.info("Metixel Photoframe v%s starting in %s mode", __version__, args.mode)

    if args.mode == "backend":
        from metixel.backend.daemon import BackendDaemon

        daemon = BackendDaemon(config_path=args.config)
        daemon.run()
    elif args.mode == "frontend":
        from metixel.frontend.renderer import FrontendRenderer

        renderer = FrontendRenderer(config_path=args.config)
        renderer.run()


if __name__ == "__main__":
    main()
