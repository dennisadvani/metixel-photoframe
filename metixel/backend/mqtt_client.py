# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""MQTT client for Home Assistant integration.

Publishes system health metrics and current media metadata.
Subscribes to control topics for remote frame control.
"""

from __future__ import annotations

import json
import logging
import time

from metixel.backend.state import StateManager
from metixel.shared.ipc import ControlMessage, IPCClient

logger = logging.getLogger(__name__)


class MQTTClient:
    """MQTT client bridge between Home Assistant and Metixel Photoframe.

    Publishes:
    - ``{prefix}/status`` — online/offline
    - ``{prefix}/health`` — system health metrics
    - ``{prefix}/current_media`` — currently displayed media info
    - ``{prefix}/state`` — playing/paused/off

    Subscribes:
    - ``{prefix}/cmd`` — control commands (next, prev, pause, power_off, etc.)
    - ``{prefix}/album/set`` — switch to a specific album
    """

    def __init__(self, state: StateManager, ipc: IPCClient) -> None:
        self._state = state
        self._ipc = ipc
        self._running = False
        self._client = None  # paho.mqtt.client.Client

    def run(self) -> None:
        """Connect to MQTT broker and start the event loop."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.warning("paho-mqtt not installed — MQTT disabled")
            return

        config = self._state.config.mqtt
        prefix = config["topic_prefix"]

        self._client = mqtt.Client(client_id=f"metixel-{id(self)}")
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        if config.get("username"):
            self._client.username_pw_set(config["username"], config.get("password", ""))

        # Set Last Will to publish offline status on disconnect
        self._client.will_set(f"{prefix}/status", "offline", retain=True)

        try:
            self._client.connect(config["broker"], config["port"], keepalive=60)
        except Exception:
            logger.error("Failed to connect to MQTT broker at %s:%d",
                        config["broker"], config["port"])
            return

        self._running = True
        self._client.publish(f"{prefix}/status", "online", retain=True)
        logger.info("MQTT client connected to %s:%d", config["broker"], config["port"])

        self._client.loop_start()

        # Publish health periodically
        while self._running:
            self._publish_health(prefix)
            time.sleep(30)

        self._client.loop_stop()
        self._client.disconnect()

    def stop(self) -> None:
        """Disconnect from MQTT broker."""
        self._running = False

    # -- Callbacks -----------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc) -> None:
        """Subscribe to control topics on connect."""
        prefix = self._state.config.mqtt["topic_prefix"]
        client.subscribe(f"{prefix}/cmd")
        client.subscribe(f"{prefix}/album/set")
        logger.debug("MQTT subscribed to %s/cmd and %s/album/set", prefix, prefix)

    def _on_message(self, client, userdata, msg) -> None:
        """Handle incoming MQTT messages."""
        payload = msg.payload.decode("utf-8", errors="replace")
        logger.debug("MQTT received: %s = %s", msg.topic, payload)

        if msg.topic.endswith("/cmd"):
            self._handle_cmd(payload)
        elif msg.topic.endswith("/album/set"):
            self._ipc.send(ControlMessage(cmd="switch_album", args={"album_id": payload}))

    def _handle_cmd(self, command: str) -> None:
        """Route MQTT command to the frontend via IPC."""
        cmd_map = {
            "next": "next",
            "prev": "prev",
            "pause": "pause",
            "resume": "resume",
            "power_off": "screen_off",
            "power_on": "screen_on",
        }
        cmd = cmd_map.get(command.lower(), "")
        if cmd:
            self._ipc.send(ControlMessage(cmd=cmd))
        else:
            logger.warning("Unknown MQTT command: %s", command)

    def _publish_health(self, prefix: str) -> None:
        """Publish system health metrics."""
        health = self._state.get_system_health()
        self._client.publish(f"{prefix}/health", json.dumps(health))
