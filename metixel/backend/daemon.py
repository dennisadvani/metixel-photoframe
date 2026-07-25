# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Backend Daemon — orchestrates sync engines, web server, MQTT, and input handlers.

This is the main entry point for the backend process. It starts all background
services and runs the Flask web server as the foreground thread.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from metixel.backend.state import StateManager
from metixel.shared.ipc import IPCClient

logger = logging.getLogger(__name__)


class BackendDaemon:
    """Main backend daemon that coordinates all background services.

    Services managed:
    - Web server (Flask) — foreground thread
    - Sync engine (Immich + folder watcher) — background threads
    - OptimisationQueue — background thread (image/video processing)
    - MQTT client — background thread
    - Input handlers (CEC, IR) — background threads
    """

    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path.resolve()
        self._state = StateManager(self._config_path)
        self._ipc = IPCClient()
        self._running = False
        self._config = self._state.config
        self._threads: list[threading.Thread] = []

    # -- Service lifecycle ---------------------------------------------------

    def run(self) -> None:
        """Start all backend services. Blocks on the web server."""
        self._running = True
        logger.info(
            "\n" + "=" * 70 + "\n"
            "  METIXEL BACKEND STARTING  |  pid=%d  |  %s\n"
            + "=" * 70,
            os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S"),
        )

        self._start_optimisation_queue()
        self._start_sync_engine()
        self._start_mqtt_client()
        self._start_input_handlers()
        self._start_web_server()

        logger.info(
            "\n" + "=" * 70 + "\n"
            "  METIXEL BACKEND STOPPING  |  pid=%d  |  %s\n"
            + "=" * 70,
            os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self._running = False
        self._ipc.close()
        self._join_threads()

    def shutdown(self) -> None:
        """Gracefully stop all services."""
        self._running = False

    # -- Service starters ----------------------------------------------------

    def _start_optimisation_queue(self) -> None:
        """Start the media optimisation queue in a background thread.

        Runs between the folder watcher (which feeds it metadata stubs)
        and the slideshow playlist (which consumes optimised items).
        Must be started BEFORE the folder watcher so the queue is
        available to receive items.
        """
        from metixel.backend.processing.optimisation_queue import OptimisationQueue

        self._opt_queue = OptimisationQueue(self._state)
        t = threading.Thread(
            target=self._opt_queue.run, name="optimisation-queue", daemon=True,
        )
        t.start()
        self._threads.append(t)
        logger.info("Optimisation queue started")

    def _start_sync_engine(self) -> None:
        """Start the media sync engine in a background thread.

        Handles both Immich API sync and local folder watching.
        The folder watcher is connected to the OptimisationQueue so
        discovered items flow through the complete pipeline.
        """
        config = self._state.config

        if config.sync.get("immich", {}).get("enabled", False):
            logger.info("Immich sync enabled — starting sync engine")
            from metixel.backend.sync.immich import ImmichSyncer

            syncer = ImmichSyncer(self._state)
            t = threading.Thread(target=syncer.run, name="immich-syncer", daemon=True)
            t.start()
            self._threads.append(t)

        if config.sync.get("local", {}).get("enabled", True):
            logger.info("Local folder sync enabled — starting folder watcher")
            from metixel.backend.sync.folder_watcher import FolderWatcher

            watcher = FolderWatcher(
                self._state,
                opt_queue=getattr(self, "_opt_queue", None),
            )
            t = threading.Thread(target=watcher.run, name="folder-watcher", daemon=True)
            t.start()
            self._threads.append(t)

    def _start_mqtt_client(self) -> None:
        """Start the MQTT client for Home Assistant integration."""
        config = self._state.config

        if config.mqtt.get("enabled", False):
            logger.info("MQTT enabled — starting client")
            from metixel.backend.mqtt_client import MQTTClient

            client = MQTTClient(self._state, self._ipc)
            t = threading.Thread(target=client.run, name="mqtt-client", daemon=True)
            t.start()
            self._threads.append(t)

    def _start_input_handlers(self) -> None:
        """Start CEC and IR input handlers."""
        config = self._state.config

        if config.input.get("cec_enabled", True):
            from metixel.backend.input_handlers.cec import CECHandler

            cec = CECHandler(self._state, self._ipc)
            t = threading.Thread(target=cec.run, name="cec-handler", daemon=True)
            t.start()
            self._threads.append(t)

        if config.input.get("ir_enabled", False):
            from metixel.backend.input_handlers.ir import IRHandler

            ir = IRHandler(self._state, self._ipc)
            t = threading.Thread(target=ir.run, name="ir-handler", daemon=True)
            t.start()
            self._threads.append(t)

    def _start_web_server(self) -> None:
        """Start the Flask web server — this BLOCKS the main thread."""
        from metixel.backend.web.server import create_app

        app = create_app(self._state, self._ipc)
        web_config = self._state.config.web

        logger.info("Web server starting on %s:%d", web_config["host"], web_config["port"])
        app.run(
            host=web_config["host"],
            port=web_config["port"],
            debug=web_config.get("debug", False),
            threaded=True,
        )

    def _join_threads(self) -> None:
        """Wait for all background threads to finish."""
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=5.0)
