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
        self._start_network_monitor()
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

    def _start_network_monitor(self) -> None:
        """Start the network monitor thread.

        Waits for the configured timeout after startup.  If no network
        connection is established by then, activates the AP fallback
        (captive portal) so the user can configure Wi-Fi.

        Once a connection is established, auto-deactivates the AP and
        dismisses the ``welcome_wifi`` persistent message (if present).
        """
        config = self._state.config
        network_cfg = config.network

        if not network_cfg.get("ap_fallback_enabled", True):
            logger.info("AP fallback disabled — skipping network monitor")
            return

        t = threading.Thread(
            target=self._network_monitor_loop,
            name="network-monitor",
            daemon=True,
        )
        t.start()
        self._threads.append(t)
        logger.info("Network monitor started (timeout=%ds)",
                     network_cfg.get("ap_timeout_seconds", 60))

    def _network_monitor_loop(self) -> None:
        """Background loop: monitor connectivity and manage AP fallback.

        All AP activations are PIN-gated.  A random 4-digit PIN is shown
        on the frame display and must be entered on the captive portal
        before Wi-Fi reconfiguration is allowed.
        """
        from metixel.backend.network_manager import (
            clear_ap_pin,
            generate_ap_pin,
            is_ap_mode_active,
            is_connected,
            start_ap_mode,
            stop_ap_mode,
        )

        config = self._state.config
        timeout = config.network.get("ap_timeout_seconds", 60)

        # Wait for the initial timeout before checking
        waited = 0
        while self._running and waited < timeout:
            time.sleep(5)
            waited += 5

        if not self._running:
            return

        if is_connected():
            logger.info("Network connected — AP fallback not needed")
            return

        # ── No connection — activate PIN-gated AP fallback ───────────
        pin = generate_ap_pin()
        logger.warning(
            "No network after %ds — activating PIN-gated AP fallback (PIN: %s)",
            timeout, pin,
        )
        self._show_pin_on_screen(pin)
        start_ap_mode()

        # Monitor for connection changes
        ap_was_active = True
        while self._running:
            time.sleep(10)
            if not self._running:
                break

            if is_connected():
                if ap_was_active:
                    logger.info("Network connected — deactivating AP fallback")
                    stop_ap_mode()
                    clear_ap_pin()
                    ap_was_active = False
                    self._dismiss_welcome_message()
                    self._dismiss_pin_message()
            else:
                if not ap_was_active and not is_ap_mode_active():
                    pin = generate_ap_pin()
                    logger.warning("Network lost — reactivating PIN-gated AP fallback (PIN: %s)", pin)
                    self._show_pin_on_screen(pin)
                    start_ap_mode()
                    ap_was_active = True

    def _show_pin_on_screen(self, pin: str) -> None:
        """Display the AP security PIN as a persistent message on the frame."""
        try:
            from metixel.shared.ipc import ControlMessage
            self._ipc.send(ControlMessage(
                cmd="show_message",
                args={
                    "title": "WiFi Setup Code",
                    "body": f"Your setup PIN is: {pin}\nEnter this code on the captive portal to reconfigure WiFi.",
                    "severity": "info",
                    "duration": 0,  # persistent
                },
            ))
            logger.info("PIN message sent to frontend")
        except Exception:
            logger.warning("Failed to send PIN message to frontend", exc_info=True)

    def _dismiss_pin_message(self) -> None:
        """Dismiss the PIN message from the frame display."""
        try:
            from metixel.shared.ipc import ControlMessage
            self._ipc.send(ControlMessage(cmd="dismiss_all_messages"))
        except Exception:
            logger.debug("Could not dismiss PIN message", exc_info=True)

    def _dismiss_welcome_message(self) -> None:
        """Remove the ``welcome_wifi`` persistent message from config.

        Called automatically when Wi-Fi connects successfully so the
        first-boot instructions disappear without requiring the user
        to visit the web dashboard.
        """
        try:
            config = self._state.config
            persistent: list[dict] = config.messages.get("persistent", [])
            new_list = [m for m in persistent if m.get("id") != "welcome_wifi"]
            if len(new_list) < len(persistent):
                self._state.update_config("messages", {"persistent": new_list})
                logger.info("Auto-dismissed welcome_wifi persistent message")
                # Also tell the frontend to clear the screen
                try:
                    from metixel.shared.ipc import ControlMessage
                    self._ipc.send(ControlMessage(cmd="dismiss_all_messages"))
                except Exception:
                    logger.debug("Could not send dismiss IPC (frontend may not be running)")
        except Exception:
            logger.warning("Failed to auto-dismiss welcome message", exc_info=True)

    def _start_web_server(self) -> None:
        """Start the Flask web server — this BLOCKS the main thread."""
        from metixel.backend.web.server import create_app

        opt_queue = getattr(self, "_opt_queue", None)
        app = create_app(self._state, self._ipc, opt_queue=opt_queue)
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
