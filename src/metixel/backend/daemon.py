# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Backend Daemon — orchestrates sync engines, web server, MQTT, and input handlers.

This is the main entry point for the backend process. It starts all background
services and runs the Flask web server as the foreground thread.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from metixel.backend.state import StateManager
from metixel.shared.ipc import IPCClient
from metixel.shared.ports import Ports

if TYPE_CHECKING:
    from metixel.backend.network_controller import NetworkController, NetworkState
    from metixel.backend.update_manager import UpdateManager

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

    def __init__(
        self,
        config_path: Path,
        ports: Ports | None = None,
        run_dir: Path | None = None,
    ) -> None:
        self._config_path = config_path.resolve()
        self._ports = ports if ports is not None else Ports()
        # Honour METIXEL_RUN_DIR (same env var the frontend uses) so desktop
        # runs and tests can use a writable run directory; /run/metixel is the
        # systemd default on the Pi.
        self._state = StateManager(
            self._config_path,
            run_dir=run_dir or Path(os.environ.get("METIXEL_RUN_DIR", "/run/metixel")),
        )
        self._ipc = IPCClient()
        self._running = False
        self._config = self._state.config
        self._threads: list[threading.Thread] = []
        self._update_mgr: UpdateManager | None = None
        # Set by the web API when the frontend signals that the
        # slideshow has started — used to defer network checks.
        self._slideshow_started = threading.Event()
        # Display power state — read by Web UI / MQTT.  Initialised from the
        # schedule so HA gets the correct state on boot (MQTT starts before
        # the scheduler thread).  Falls back to True when schedule disabled.
        self._display_on: bool = self._display_should_be_on()
        # The MQTT client (set in _start_mqtt_client) — used by
        # set_display_power() to push screen-state changes to HA immediately.
        self._mqtt_client = None

    # -- Service lifecycle ---------------------------------------------------

    def run(self) -> None:
        """Start all backend services. Blocks on the web server."""
        self._running = True
        logger.info(
            "\n" + "=" * 70 + "\n  METIXEL BACKEND STARTING  |  pid=%d  |  %s\n" + "=" * 70,
            os.getpid(),
            time.strftime("%Y-%m-%d %H:%M:%S"),
        )

        self._start_optimisation_queue()
        self._start_sync_engine()
        self._start_mqtt_client()
        self._start_input_handlers()
        self._start_network_monitor()
        self._start_update_manager()
        self._start_display_scheduler()
        self._start_web_server()

        logger.info(
            "\n" + "=" * 70 + "\n  METIXEL BACKEND STOPPING  |  pid=%d  |  %s\n" + "=" * 70,
            os.getpid(),
            time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self._running = False
        self._ipc.close()
        self._join_threads()

    def shutdown(self) -> None:
        """Gracefully stop all services."""
        self._running = False
        if self._update_mgr is not None:
            with contextlib.suppress(Exception):
                self._update_mgr.shutdown()

    def reset_pipeline(self) -> None:
        """Clear all queues and restart the media pipeline from scratch.

        Called when config changes affect what media is playable
        (watch folders, video playback toggle, resolution thresholds).
        Triggers the folder watcher to re-scan on its next cycle.
        """
        logger.info("Pipeline reset requested — clearing all queues")

        # 1. Clear the slideshow playlist
        self._state.clear_playlist()

        # 2. Drain the optimisation queue (all pending items)
        opt_queue = getattr(self, "_opt_queue", None)
        if opt_queue is not None:
            opt_queue.pause()
            logger.info("Optimisation queue drained")

        # 3. Reset folder watcher snapshot so the next scan
        #    re-discovers all files with the new config
        watcher = getattr(self, "_folder_watcher", None)
        if watcher is not None:
            watcher.reset_snapshot()
            logger.info("Folder watcher snapshot reset — will re-scan")

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
            target=self._opt_queue.run,
            name="optimisation-queue",
            daemon=True,
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

        # Always start the Immich syncer thread — it performs the one-time
        # legacy layout migration at startup, and the ``enabled`` flag is
        # re-read every cycle (hot-reload), so enabling Immich sync from
        # the web UI takes effect without a backend restart.
        logger.info("Starting Immich sync engine")
        from metixel.backend.sync.immich import ImmichSyncer

        syncer = ImmichSyncer(self._state, http=self._ports.http)
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
            self._folder_watcher = watcher
            t = threading.Thread(target=watcher.run, name="folder-watcher", daemon=True)
            t.start()
            self._threads.append(t)

    def _start_mqtt_client(self) -> None:
        """Start the MQTT client for Home Assistant integration."""
        config = self._state.config

        if config.mqtt.get("enabled", False):
            logger.info("MQTT enabled — starting client")
            from metixel.backend.mqtt_client import MQTTClient

            # Pass the daemon so the MQTT client can expose the real
            # screen-power state to Home Assistant.
            client = MQTTClient(self._state, self._ipc, mqtt=self._ports.mqtt, daemon=self)
            # Keep a reference so the web UI can report broker status.
            self._mqtt_client = client
            t = threading.Thread(target=client.run, name="mqtt-client", daemon=True)
            t.start()
            self._threads.append(t)

    def set_display_power(self, on: bool, source: str = "") -> None:
        """Set the display-power state and notify every consumer.

        Single choke-point for ALL display-power changes (Web UI button,
        display scheduler, keyboard/CEC/IR remotes, MQTT commands).  It
        updates the daemon's flag, sends the ``screen_on``/``screen_off``
        IPC command to the frontend, and publishes the new state to MQTT
        immediately so Home Assistant's switch reflects reality regardless
        of which input changed it (no waiting for the 30s periodic publish).
        """
        self._display_on = bool(on)
        from metixel.shared.ipc import ControlMessage

        self._ipc.send(ControlMessage(cmd="screen_on" if on else "screen_off"))
        if self._mqtt_client is not None:
            self._mqtt_client.publish_screen_now()
        if source:
            logger.info("Display power set to %s (%s)", "ON" if on else "OFF", source)

    def _start_input_handlers(self) -> None:
        """Start CEC and IR input handlers."""
        config = self._state.config

        if config.input.get("cec_enabled", True):
            from metixel.backend.input_handlers.cec import CECHandler

            cec = CECHandler(
                self._state,
                self._ipc,
                cec=self._ports.cec,
                display_power=self.set_display_power,
            )
            t = threading.Thread(target=cec.run, name="cec-handler", daemon=True)
            t.start()
            self._threads.append(t)

        if config.input.get("ir_enabled", False):
            from metixel.backend.input_handlers.ir import IRHandler

            ir = IRHandler(
                self._state,
                self._ipc,
                ir=self._ports.ir,
                display_power=self.set_display_power,
            )
            t = threading.Thread(target=ir.run, name="ir-handler", daemon=True)
            t.start()
            self._threads.append(t)

        if config.input.get("keyboard_enabled", True):
            from metixel.backend.input_handlers.keyboard import KeyboardHandler

            self._keyboard_handler = KeyboardHandler(
                config=config.input,
                ipc=self._ipc,
                display_power=self.set_display_power,
            )
            t = threading.Thread(
                target=self._keyboard_handler.run,
                name="kbd-handler",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
            logger.info("Keyboard input handler started")

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
        logger.info(
            "Network monitor started (timeout=%ds)", network_cfg.get("ap_timeout_seconds", 60)
        )

    def _network_monitor_loop(self) -> None:
        """Background loop: monitor connectivity and manage AP fallback.

        Delegates all state decisions to :class:`NetworkController` so
        the monitor thread and Flask request threads share a single
        source of truth for PIN/AP state.  The controller returns a
        list of *pending actions* — state transitions that happened
        (possibly on another thread) — and this loop drains them.
        """
        from metixel.backend.network_controller import (
            NetworkController,
            NetworkState,
        )

        config = self._state.config
        controller = NetworkController(config.network)
        self._network_controller = controller  # Expose for web routes

        timeout = config.network.get("ap_timeout_seconds", 60)

        # ── Wait for slideshow start signal ────────────────────────
        logger.info("Network monitor waiting for slideshow start signal")
        self._slideshow_started.wait(timeout=300.0)
        if not self._running:
            return
        logger.info(
            "Slideshow started — network monitor beginning countdown (%ds)",
            timeout,
        )

        # Give the boot screen time to finish its fade-out animation
        # before showing any messages (welcome, PIN, etc.).  The fade
        # takes ~0.8s — 2s is a safe buffer.
        time.sleep(10.0)

        # ── Initial boot: only wait if NOT already connected ──────
        # If Ethernet or saved WiFi is already up, show the welcome
        # message immediately.  Only delay when there's no network
        # (giving NetworkManager time to auto-connect saved WiFi).
        state, pin, actions = controller.tick()
        if state != NetworkState.CLIENT_CONNECTED:
            logger.info(
                "No network at boot — waiting %ds for auto-connect",
                timeout,
            )
            waited = 0
            while self._running and waited < timeout:
                time.sleep(5)
                waited += 5
                # Stop early if WiFi connects during the wait
                state, pin, actions = controller.tick()
                if state == NetworkState.CLIENT_CONNECTED:
                    logger.info("Network connected during countdown")
                    break

            if not self._running:
                return

        # Re-evaluate state after any wait
        state, pin, actions = controller.tick()

        if state == NetworkState.CLIENT_CONNECTED:
            logger.info("Network connected — AP fallback not needed")
            self._show_first_run_welcome()
        elif state == NetworkState.AP_ACTIVE and pin:
            logger.warning(
                "No network after %ds — activating PIN-gated AP fallback (PIN: %s)",
                timeout,
                pin,
            )
            self._show_pin_on_screen(pin)
            controller.mark_pin_displayed()

        # ── Drain initial actions (usually empty, but safe) ────────
        self._drain_actions(controller, actions)

        # ── Main monitoring loop ───────────────────────────────────
        while self._running:
            time.sleep(5)
            if not self._running:
                break

            state, pin, actions = controller.tick()
            self._drain_actions(controller, actions)

            # Safety net: dismiss PIN if we're connected but the
            # overlay is still showing (covers IPC delivery failures).
            if state == NetworkState.CLIENT_CONNECTED and controller.pin_displayed:
                self._dismiss_pin_message()
                controller.mark_pin_dismissed()

    # -- Action drainer ----------------------------------------------------

    def _drain_actions(
        self,
        controller: NetworkController,
        actions: list[NetworkState],
    ) -> None:
        """Execute side effects for each pending state transition."""
        from metixel.backend.network_controller import NetworkState

        for action in actions:
            if action == NetworkState.CLIENT_CONNECTED:
                logger.info("Network connected — deactivating AP fallback")
                self._dismiss_pin_message()
                controller.mark_pin_dismissed()
                self._show_connected_message()
                self._show_first_run_welcome()

            elif action == NetworkState.AP_ACTIVE:
                pin = controller.pin
                logger.warning("Activating AP fallback (PIN: %s)", pin)
                if pin:
                    self._show_pin_on_screen(pin)
                    controller.mark_pin_displayed()

            elif action == NetworkState.AP_EXHAUSTED:
                logger.info("AP exhausted — will not reactivate until reboot")
                self._dismiss_pin_message()
                controller.mark_pin_dismissed()
                try:
                    from metixel.shared.ipc import ControlMessage

                    self._ipc.send(
                        ControlMessage(
                            cmd="show_message",
                            args={
                                "title": "WiFi Offline",
                                "body": (
                                    "Could not reconnect. The WiFi setup portal "
                                    "will not appear again until the frame is "
                                    "rebooted."
                                ),
                                "severity": "warning",
                                "duration": 120,
                            },
                        )
                    )
                except Exception:
                    pass

            elif action == NetworkState.CLIENT_DISCONNECTED:
                logger.info("Network lost — grace period active")
                # No UI action — just waiting.  The boot layer or an
                # existing message can stay on screen.

    # -- On-screen helpers ------------------------------------------------

    def _show_pin_on_screen(self, pin: str) -> None:
        """Display the AP security PIN as a persistent message on the frame.

        Dismisses any existing messages first so the PIN doesn't stack
        on top of the welcome message or other persistent overlays.
        """
        try:
            from metixel.shared.ipc import ControlMessage

            # Clear existing messages before showing PIN
            self._ipc.send(ControlMessage(cmd="dismiss_all_messages"))
            time.sleep(0.3)  # Brief pause so frontend processes dismiss
            self._ipc.send(
                ControlMessage(
                    cmd="show_message",
                    args={
                        "title": "Welcome to Metixel!",
                        "body": (
                            f"No network connection detected. "
                            f"To configure one, connect to 'Metixel-Setup' WiFi, "
                            f"open http://192.168.42.1 or http://metixel.local "
                            f"and use PIN {pin} to login."
                        ),
                        "severity": "info",
                        "duration": 0,  # persistent
                    },
                )
            )
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

    def _show_connected_message(self) -> None:
        """Show a post-connection confirmation on the frame display."""
        try:
            # Respect the suppress-popups config setting
            config = self._state.config
            if not config.messages.get("enabled", True):
                return

            from metixel.backend.network_manager import get_connection_status

            status = get_connection_status()
            ip = status.get("ip", "")
            if not ip or ip.startswith("192.168.42."):
                logger.debug("No real IP — skipping connected message")
                return
            iface_type = status.get("interface_type", "")
            label = "WiFi" if iface_type == "wifi" else "Ethernet"

            from metixel.shared.ipc import ControlMessage

            self._ipc.send(
                ControlMessage(
                    cmd="show_message",
                    args={
                        "title": f"Connected via {label}",
                        "body": (
                            f"{label} connected. Access Metixel at "
                            f"http://metixel.local or http://{ip}"
                        ),
                        "severity": "success",
                        "duration": 60,  # auto-dismiss after 60s
                    },
                )
            )
            logger.info("Connected message sent to frontend")
        except Exception:
            logger.warning("Failed to show connected message", exc_info=True)

    def _show_first_run_welcome(self) -> None:
        """Show a first-run welcome overlay on the frame display.

        Only triggers when ``system.first_run`` is ``True`` in config.
        After showing the message, sets the flag to ``False`` so the
        welcome is never shown again.

        If the frontend is not yet running (IPC send fails), the flag
        is NOT cleared — the web dashboard will still show its welcome
        banner on first access.
        """
        try:
            config = self._state.config
            if not config.system.get("first_run", False):
                return

            from metixel.backend.network_manager import get_connection_status

            status = get_connection_status()
            ip = status.get("ip", "")

            # Don't show welcome messages if there's no real IP —
            # is_connected() can return false positives under CPU load
            # when nmcli times out.
            if not ip or ip.startswith("192.168.42."):
                logger.debug("No real IP — skipping first-run welcome")
                return

            from metixel.shared.ipc import ControlMessage

            # Show three welcome messages sequentially on the frame display.
            # Each auto-dismisses after 2 minutes.
            messages = [
                {
                    "title": "Welcome to Metixel",
                    "body": f"Manage your photo frame via the Web UI at http://metixel.local or http://{ip}.",
                },
                {
                    "title": "Upload photos via File Sharing",
                    "body": (
                        "Upload photos and videos to your media folder via SMB — "
                        "Windows: \\\\metixel\\metixel-media, "
                        "Mac: smb://metixel/metixel-media. "
                        "Or sync your Immich media to this photo frame via the Web UI."
                    ),
                },
                {
                    "title": "Enjoy Metixel!",
                    "body": (
                        "These messages will dismiss in 2 minutes. "
                        "Remove these messages in future by dismissing the "
                        "welcome banner in the Web UI. You can also remove all the "
                        "boot up messages in Advanced → System → Quiet Boot."
                    ),
                },
            ]

            for msg in messages:
                self._ipc.send(
                    ControlMessage(
                        cmd="show_message",
                        args={**msg, "severity": "info", "duration": 120},
                    )
                )
                time.sleep(0.5)  # brief pause so messages queue in order

            logger.info("First-run welcome messages sent to frontend")
        except Exception:
            logger.warning("Failed to show first-run welcome", exc_info=True)

    def _start_update_manager(self) -> None:
        """Start the OTA update manager in a background thread.

        Periodically checks GitHub for new versions on the configured
        channel.  Must be started BEFORE the web server so the
        UpdateManager is available to the API routes.
        """
        from metixel.backend.update_manager import UpdateManager

        self._update_mgr = UpdateManager(self._state, http=self._ports.http)
        t = threading.Thread(
            target=self._update_mgr.run,
            name="update-manager",
            daemon=True,
        )
        t.start()
        self._threads.append(t)
        logger.info("Update manager started")

    def _display_should_be_on(self) -> bool:
        """Whether the display should be on right now per the schedule.

        Returns ``True`` when the schedule is disabled (display stays on)
        or the current time falls inside the on-window.
        """
        config = self._state.config
        if not config.display.get("schedule_enabled", False):
            return True
        on_str = config.display.get("schedule_on_time", "07:00")
        off_str = config.display.get("schedule_off_time", "22:00")

        def _parse_time(t: str) -> int:
            parts = t.strip().split(":")
            return int(parts[0]) * 60 + int(parts[1])

        now = time.localtime()
        now_minutes = now.tm_hour * 60 + now.tm_min
        on_min = _parse_time(on_str)
        off_min = _parse_time(off_str)
        return on_min <= now_minutes < off_min

    def _start_display_scheduler(self) -> None:
        """Start the display power scheduler in a background thread.

        Checks the configured on/off schedule every 30 seconds and sends
        ``screen_on`` / ``screen_off`` IPC commands to the frontend when
        the display should change state.

        The display-power flag is initialised from the schedule in
        ``__init__`` (before MQTT connects) so Home Assistant receives the
        correct initial screen state on boot.
        """

        def _scheduler_loop() -> None:
            logger.info("Display scheduler started")
            last_state: bool | None = None  # None on first iteration

            while self._running:
                try:
                    if not self._state.config.display.get("schedule_enabled", False):
                        time.sleep(30)
                        continue

                    should_be_on = self._display_should_be_on()
                    if should_be_on != last_state:
                        last_state = should_be_on
                        # Route through the single choke-point so the flag,
                        # the frontend IPC, and the immediate MQTT publish all
                        # stay in sync with every other display-power source.
                        self.set_display_power(should_be_on, source="schedule")
                except Exception:
                    logger.debug("Display scheduler error", exc_info=True)

                time.sleep(30)

        t = threading.Thread(
            target=_scheduler_loop,
            name="display-scheduler",
            daemon=True,
        )
        t.start()
        self._threads.append(t)
        logger.info("Display scheduler started")

    def _start_web_server(self) -> None:
        """Start the Flask web server — this BLOCKS the main thread."""
        from metixel.backend.web.server import create_app

        opt_queue = getattr(self, "_opt_queue", None)
        update_mgr = getattr(self, "_update_mgr", None)
        app = create_app(
            self._state, self._ipc, opt_queue=opt_queue, update_mgr=update_mgr, daemon=self
        )
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


def build_backend(
    config_path: Path,
    ports: Ports | None = None,
    run_dir: Path | None = None,
) -> BackendDaemon:
    """Composition root for the backend process.

    Constructs :class:`BackendDaemon` with its external dependencies.
    Ports left ``None`` resolve to the real adapters inside each service
    (default behaviour); tests and alternate deployments inject fakes.
    """
    return BackendDaemon(config_path, ports=ports, run_dir=run_dir)
