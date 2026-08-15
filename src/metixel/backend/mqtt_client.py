# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""MQTT client for Home Assistant integration.

Publishes system health metrics, current media metadata, playback state, and
screen-power state.  Optionally publishes Home Assistant MQTT Discovery configs
so HA auto-discovers Metixel as a device with buttons, a screen-power switch,
and sensors.

Subscribes to control topics for remote frame control.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import time
from typing import Any

from metixel import __version__
from metixel.backend.state import StateManager
from metixel.shared.ipc import ControlMessage, IPCClient
from metixel.shared.paths import run_path
from metixel.shared.platform import resolve_unique_id
from metixel.shared.ports import MqttGateway

logger = logging.getLogger(__name__)

# Discovery configs are re-published on this interval so HA re-adopts
# entities if they were removed (e.g. after a broker restart).
_DISCOVERY_REPUBLISH_SECONDS = 30 * 60
# The frontend writes its current-media state here (see PresentationEngine).
_CURRENT_MEDIA_FILE = run_path("current_media.json")


class MQTTClient:
    """MQTT client bridge between Home Assistant and Metixel Photoframe.

    All topics are scoped by a per-frame identifier so multiple frames on one
    broker never collide: ``metixel/<device_id>/...`` where ``<device_id>``
    comes from ``mqtt.device_id`` (default: a hardware-unique id derived from
    the Pi serial / MAC / machine-id).  ``prefix`` below = ``metixel/<device_id>``.

    Raw topics (always published when MQTT is enabled):
    - ``{prefix}/status`` — online/offline (retained)
    - ``{prefix}/health`` — system health JSON
    - ``{prefix}/current_media`` — current media JSON (title/media_type/paused)
    - ``{prefix}/state`` — playing/paused/off
    - ``{prefix}/screen`` — ON/OFF (screen power)
    - ``{prefix}/cmd`` — command input (next, prev, pause, resume, toggle_pause,
      power_on, power_off)
    - ``{prefix}/album/set`` — album switch input
    - ``{prefix}/screen/set`` — screen power input (ON/OFF)

    Home Assistant MQTT Discovery (opt-in via ``mqtt.discovery_enabled``):
    publishes retained configs to ``{discovery_prefix}/<component>/metixel_<entity>/config``
    for buttons, a screen-power switch, and sensors.
    """

    def __init__(
        self,
        state: StateManager,
        ipc: IPCClient,
        mqtt: MqttGateway | None = None,
        daemon: Any | None = None,
    ) -> None:
        self._state = state
        self._ipc = ipc
        # Used to read/update the screen-power state flag (see daemon._display_on).
        self._daemon = daemon
        self._running = False
        self._mqtt = mqtt  # injected MqttGateway port (None → real adapter in run())
        self._connected = False  # True only after the broker CONNACK succeeds
        self._reject_warned = False  # avoid log spam on repeated auth failures
        self._last_discovery_publish: float = 0.0

        # Broker connection state for the dashboard (see ``status()``).
        self._status: str = "idle"  # idle | connecting | connected | rejected
        self._last_error: str | None = None
        self._connecting_since: float | None = None

    def _topic_prefix(self) -> str:
        """Full MQTT topic prefix for this frame: ``metixel/<device_id>``.

        The topic namespace is ALWAYS scoped by the per-frame device id so
        two frames on one broker never share topics (sensors, commands, or
        screen state).  The legacy ``mqtt.topic_prefix`` config key is no
        longer read — every frame uses the same ``metixel`` base plus its
        unique id.
        """
        return f"metixel/{self._resolve_device_id()}"

    def run(self) -> None:
        """Connect to MQTT broker and start the event loop."""
        gw = self._mqtt
        if gw is None:
            try:
                from metixel.shared.adapters import PahoMqttGateway

                gw = PahoMqttGateway()
                self._mqtt = gw
            except ImportError:
                logger.warning("paho-mqtt not installed — MQTT disabled")
                return

        config = self._state.config.mqtt
        prefix = self._topic_prefix()
        self._broker_host = config["broker"]
        self._broker_port = config["port"]

        if config.get("username"):
            gw.set_credentials(config["username"], config.get("password", ""))

        # Set Last Will to publish offline status on disconnect
        gw.set_will(f"{prefix}/status", "offline", retain=True)
        gw.set_handlers(self._on_connect, self._on_message)

        # connect() is non-blocking — the CONNACK (and whether authentication
        # succeeded) arrives asynchronously and is handled in _on_connect.
        try:
            gw.connect(self._broker_host, self._broker_port, keepalive=60)
        except Exception:  # noqa: BLE001
            logger.error(
                "Failed to connect to MQTT broker at %s:%d",
                self._broker_host,
                self._broker_port,
            )
            return

        self._running = True
        self._status = "connecting"
        self._connecting_since = time.monotonic()
        logger.info(
            "MQTT client starting — connecting to %s:%d",
            self._broker_host,
            self._broker_port,
        )
        gw.loop_start()
        self._last_discovery_publish = time.monotonic()
        self._last_health_publish = time.monotonic()
        self._last_media_sig = self._current_media_sig()

        # Publish health / screen periodically, re-publish discovery so HA
        # re-adopts entities if they were removed, and republish the media /
        # playback state IMMEDIATELY when the frontend rewrites its state file.
        # All publishing is gated on _connected (the broker accepted us).
        while self._running:
            if self._connected:
                now = time.monotonic()

                # Playback state: the frontend writes current_media.json on
                # every pause/resume/next/prev/advance.  Watching the file's
                # signature lets us push the new state to HA without waiting
                # for the periodic tick — from ANY invocation source (MQTT
                # command, Web UI, keyboard/CEC/IR, or the slideshow timer).
                media_sig = self._current_media_sig()
                if media_sig != self._last_media_sig:
                    self.publish_media_now()
                    self._last_media_sig = media_sig

                # Health + screen every 30s (screen also has publish_screen_now).
                if now - self._last_health_publish >= 30.0:
                    self._publish_health(prefix)
                    self._publish_screen(prefix)
                    self._last_health_publish = now

                # Discovery every 30 minutes.
                if now - self._last_discovery_publish >= _DISCOVERY_REPUBLISH_SECONDS:
                    self._publish_discovery(prefix)
                    self._last_discovery_publish = now
            time.sleep(1)

        gw.loop_stop()
        gw.disconnect()

    def stop(self) -> None:
        """Disconnect from MQTT broker."""
        self._running = False

    def status(self) -> dict[str, Any]:
        """Report the current broker connection state for the dashboard.

        Returns one of:
        - ``disabled`` — MQTT is not enabled in config
        - ``connected`` — broker accepted us (CONNACK 0)
        - ``auth_error`` — broker rejected the connection (e.g. bad credentials)
        - ``connecting`` — connect attempt in progress
        - ``not_responding`` — connect attempt stuck for 15+ seconds
        - ``disconnected`` — not connected for another reason
        """
        cfg = self._state.config.mqtt
        enabled = bool(cfg.get("enabled", False))
        broker = getattr(self, "_broker_host", None) or cfg.get("broker", "localhost")
        port = getattr(self, "_broker_port", None) or int(cfg.get("port", 1883))
        base: dict[str, Any] = {"enabled": enabled, "broker": broker, "port": port}

        if not enabled:
            return {**base, "status": "disabled"}
        if self._connected:
            return {**base, "status": "connected"}
        if self._status == "rejected":
            return {**base, "status": "auth_error", "error": self._last_error}
        if self._status == "connecting":
            stuck = (
                self._connecting_since is not None
                and (time.monotonic() - self._connecting_since) > 15
            )
            if stuck:
                return {**base, "status": "not_responding"}
            return {**base, "status": "connecting"}
        return {**base, "status": "disconnected"}

    # -- Callbacks -----------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, *extra) -> None:
        """Handle the broker CONNACK.

        paho 1.x passes ``rc`` as an int; paho 2.x passes a ``ReasonCode``.
        Only subscribe and publish discovery/state when the connection was
        actually accepted (``reason_code == 0``).  A rejected connection
        (bad credentials, broker full, …) must NOT publish anything — the
        broker is refusing us, and the client is in a reconnect-retry loop.
        """
        prefix = self._topic_prefix()
        gw = self._mqtt
        if gw is None:
            return

        if reason_code != 0:
            was_connected = self._connected
            self._connected = False
            self._status = "rejected"
            self._last_error = str(reason_code)
            if was_connected:
                logger.warning("MQTT broker connection lost (reason_code=%s)", reason_code)
                self._reject_warned = False
            elif not self._reject_warned:
                logger.warning(
                    "MQTT broker rejected connection (reason_code=%s) — "
                    "check the broker address and credentials",
                    reason_code,
                )
                self._reject_warned = True
            return

        self._connected = True
        self._status = "connected"
        self._last_error = None
        self._reject_warned = False
        logger.info(
            "MQTT client connected to %s:%d",
            getattr(self, "_broker_host", "?"),
            getattr(self, "_broker_port", 0),
        )
        gw.subscribe(f"{prefix}/cmd")
        gw.subscribe(f"{prefix}/album/set")
        gw.subscribe(f"{prefix}/screen/set")
        logger.debug(
            "MQTT subscribed to %s/cmd, %s/album/set, %s/screen/set", prefix, prefix, prefix
        )
        # Online status + initial state so HA entities become available.
        gw.publish(f"{prefix}/status", "online", retain=True)
        self._publish_discovery(prefix)
        self._publish_screen(prefix)
        self._publish_media(prefix)

    def _on_message(self, client, userdata, msg) -> None:
        """Handle incoming MQTT messages."""
        payload = msg.payload.decode("utf-8", errors="replace")
        logger.debug("MQTT received: %s = %s", msg.topic, payload)

        if msg.topic.endswith("/cmd"):
            self._handle_cmd(payload)
        elif msg.topic.endswith("/album/set"):
            self._ipc.send(ControlMessage(cmd="switch_album", args={"album_id": payload}))
        elif msg.topic.endswith("/screen/set"):
            self._handle_screen_cmd(payload)

    def _handle_cmd(self, command: str) -> None:
        """Route MQTT command to the frontend via IPC."""
        cmd_map = {
            "next": "next",
            "prev": "prev",
            "pause": "pause",
            "resume": "resume",
            "toggle_pause": "toggle_pause",
            "power_off": "screen_off",
            "power_on": "screen_on",
        }
        cmd = cmd_map.get(command.lower(), "")
        if not cmd:
            logger.warning("Unknown MQTT command: %s", command)
            return
        if cmd in ("screen_off", "screen_on"):
            # Route through the daemon choke-point so the flag, the frontend
            # IPC, and the immediate MQTT publish stay in sync with every
            # other source (Web UI, scheduler, keyboard/CEC/IR).  Fall back
            # to a direct IPC send + flag update + publish when there is no
            # daemon (e.g. unit tests).
            if self._daemon is not None and hasattr(self._daemon, "set_display_power"):
                self._daemon.set_display_power(cmd == "screen_on", source="mqtt")
            else:
                self._ipc.send(ControlMessage(cmd=cmd))
                self._set_display_on(cmd == "screen_on")
                self.publish_screen_now()
            return
        self._ipc.send(ControlMessage(cmd=cmd))

    def _handle_screen_cmd(self, payload: str) -> None:
        """Handle the screen-power switch input (ON/OFF)."""
        value = payload.strip().upper()
        if value == "ON":
            self._handle_cmd("power_on")
        elif value == "OFF":
            self._handle_cmd("power_off")
        else:
            logger.warning("Unknown screen power value: %s", payload)

    # -- Publishing ----------------------------------------------------------

    def _publish_health(self, prefix: str) -> None:
        """Publish system health metrics."""
        health = self._state.get_system_health()
        gw = self._mqtt
        if gw is not None:
            gw.publish(f"{prefix}/health", json.dumps(health))

    def _publish_media(self, prefix: str) -> None:
        """Publish current media + playback state (from the frontend's state file)."""
        gw = self._mqtt
        if gw is None:
            return
        data = self._current_media_data()
        gw.publish(f"{prefix}/current_media", json.dumps(data))
        gw.publish(f"{prefix}/state", data.get("state", "off"))

    def publish_screen_now(self) -> None:
        """Publish the current screen-power state immediately.

        Called by the daemon's ``set_display_power`` choke-point whenever the
        display power changes from ANY source (Web UI button, display
        scheduler, keyboard/CEC/IR remotes, MQTT commands) so Home
        Assistant's switch reflects reality without waiting for the periodic
        (30s) state publish.
        """
        self._publish_screen(self._topic_prefix())

    def publish_media_now(self) -> None:
        """Publish the current media + playback state immediately.

        Mirrors ``publish_screen_now()`` for the playback side.  Called
        whenever the frontend rewrites its current-media state file (pause /
        resume / next / prev / advance), so Home Assistant's playback sensor
        reflects the change without waiting for the periodic (30s) state
        publish — regardless of which source triggered the state change (MQTT
        command, Web UI, keyboard/CEC/IR, or the slideshow timer).
        """
        self._publish_media(self._topic_prefix())

    def _publish_screen(self, prefix: str) -> None:
        """Publish the screen-power switch state (ON/OFF)."""
        gw = self._mqtt
        if gw is None:
            return
        gw.publish(f"{prefix}/screen", "ON" if self._display_on() else "OFF")

    def _display_on(self) -> bool:
        """Current screen power state (defaults to on)."""
        if self._daemon is None:
            return True
        return bool(getattr(self._daemon, "_display_on", True))

    def _set_display_on(self, value: bool) -> None:
        """Update the daemon's screen-power state flag."""
        if self._daemon is not None:
            self._daemon._display_on = value  # noqa: SLF001

    @staticmethod
    def _current_media_sig() -> tuple[int, int] | None:
        """Cheap signature of the frontend's current-media state file.

        Returns ``(st_mtime_ns, st_size)`` (sub-second writes toggling the
        ``paused`` flag are still detected) or ``None`` when the file is
        absent.  Used by the client loop to republish playback state as soon
        as the frontend changes it.
        """
        try:
            st = os.stat(_CURRENT_MEDIA_FILE)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _current_media_data(self) -> dict[str, Any]:
        """Read the frontend's current-media state file into a safe dict."""
        data: dict[str, Any] = {
            "file": None,
            "title": "none",
            "media_type": None,
            "paused": False,
            "state": "off",
        }
        try:
            if os.path.isfile(_CURRENT_MEDIA_FILE):
                with open(_CURRENT_MEDIA_FILE, encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    data.update(raw)
        except (OSError, ValueError):
            pass
        # Derive a friendly title + playback state.
        fname = data.get("file")
        data["title"] = os.path.basename(str(fname)) if fname else "none"
        if not data.get("file"):
            data["state"] = "off"
        elif data.get("paused"):
            data["state"] = "paused"
        else:
            data["state"] = "playing"
        return data

    # -- Home Assistant MQTT Discovery --------------------------------------

    def _publish_discovery(self, prefix: str) -> None:
        """Publish retained HA discovery configs (opt-in via config)."""
        gw = self._mqtt
        config = self._state.config.mqtt
        if gw is None or not config.get("discovery_enabled", True):
            return
        base = config.get("discovery_prefix", "homeassistant").rstrip("/")
        device = self._build_device()
        configs = self._discovery_configs(prefix, device)
        for entity_id, payload in configs.items():
            gw.publish(f"{base}/{entity_id}", json.dumps(payload), retain=True)
        logger.info("Published %d HA MQTT discovery config(s)", len(configs))

    def _build_device(self) -> dict[str, Any]:
        """Build the shared Home Assistant device block.

        The device identity (identifiers) is scoped by ``mqtt.device_id``
        (defaults to the hostname) so multiple frames on one broker appear
        as separate HA devices.
        """
        device_id = self._resolve_device_id()
        hostname = socket.gethostname() or "metixel"
        model = "Raspberry Pi"
        try:
            from metixel.shared.platform import detect_pi_model

            model = detect_pi_model() or model
        except Exception:  # noqa: BLE001
            pass
        return {
            "identifiers": [f"metixel_{device_id}"],
            "name": "Metixel Photo Frame",
            "manufacturer": "Metixel",
            "model": model,
            "sw_version": __version__,
            "configuration_url": f"http://{hostname}:8080",
        }

    def _resolve_device_id(self) -> str:
        """Resolve the per-frame HA device id (config value or hardware id).

        When ``mqtt.device_id`` is empty (the default), falls back to a
        stable hardware-unique identifier (Pi serial → MAC → machine-id →
        hostname) so two frames on one broker never collide even with a
        cloned SD card or default config.  Sanitised to ``[A-Za-z0-9_-]`` so
        it is safe to use inside MQTT topics and HA object IDs.
        """
        config = self._state.config.mqtt
        device_id = (config.get("device_id") or "").strip()
        if not device_id:
            device_id = resolve_unique_id()
        return re.sub(r"[^A-Za-z0-9_-]", "_", device_id)

    def _discovery_configs(self, prefix: str, device: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Build ``{component}/metixel_<entity>/config`` → payload for HA discovery.

        Every entity shares the retained ``{prefix}/status`` topic for
        availability (online/offline).
        """
        availability = {
            "availability_topic": f"{prefix}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        device_id = self._resolve_device_id()
        out: dict[str, dict[str, Any]] = {}

        def add(component: str, entity: str, name: str, payload: dict[str, Any]) -> None:
            payload.update(availability)
            payload["device"] = device
            payload["unique_id"] = f"metixel_{device_id}_{entity}"
            payload["name"] = name
            out[f"{component}/metixel_{device_id}_{entity}/config"] = payload

        # Buttons
        add(
            "button",
            "next",
            "Metixel Next",
            {"command_topic": f"{prefix}/cmd", "payload_press": "next"},
        )
        add(
            "button",
            "prev",
            "Metixel Previous",
            {"command_topic": f"{prefix}/cmd", "payload_press": "prev"},
        )
        add(
            "button",
            "pause_toggle",
            "Metixel Pause/Resume",
            {"command_topic": f"{prefix}/cmd", "payload_press": "toggle_pause"},
        )

        # Screen power switch (real ON/OFF state)
        add(
            "switch",
            "screen_power",
            "Metixel Screen Power",
            {
                "command_topic": f"{prefix}/screen/set",
                "state_topic": f"{prefix}/screen",
                "payload_on": "ON",
                "payload_off": "OFF",
                "icon": "mdi:monitor",
            },
        )

        # NOTE: no `select` entity.  A select implies a persistent settable
        # value, but next/prev/pause are momentary commands with no state to
        # report (HA shows such a select as "unknown").  The button entities
        # above already cover those commands.

        # Current media sensor (JSON attributes + title value).
        # Diagnostic + disabled by default — the raw file name is noise for
        # most users; enable it in HA if you want to see what's playing.
        add(
            "sensor",
            "current_media",
            "Metixel Current Media",
            {
                "state_topic": f"{prefix}/current_media",
                "value_template": "{{ value_json.title | default('none') }}",
                "json_attributes_topic": f"{prefix}/current_media",
                "entity_category": "diagnostic",
                "enabled_by_default": False,
                "icon": "mdi:image",
            },
        )

        # Playback state sensor
        add(
            "sensor",
            "playback_state",
            "Metixel Playback State",
            {
                "state_topic": f"{prefix}/state",
                "entity_category": "diagnostic",
                "icon": "mdi:play-circle-outline",
            },
        )

        # Health-derived sensors
        add(
            "sensor",
            "uptime",
            "Metixel Uptime",
            {
                "state_topic": f"{prefix}/health",
                # Friendly human-readable uptime (e.g. "2d 3h 45m", "3h 45m").
                "value_template": (
                    "{% set s = value_json.uptime_seconds | int %}"
                    "{% set d = s // 86400 %}"
                    "{% set h = s % 86400 // 3600 %}"
                    "{% set m = s % 3600 // 60 %}"
                    "{% if d %}{{ d }}d {{ h }}h {{ m }}m"
                    "{% elif h %}{{ h }}h {{ m }}m"
                    "{% else %}{{ m }}m{% endif %}"
                ),
                "entity_category": "diagnostic",
                "icon": "mdi:clock-outline",
            },
        )
        add(
            "sensor",
            "disk_used",
            "Metixel Disk Used",
            {
                "state_topic": f"{prefix}/health",
                "value_template": "{{ value_json.disk_used_percent | default(0) }}",
                "unit_of_measurement": "%",
                "entity_category": "diagnostic",
                "icon": "mdi:harddisk",
            },
        )
        add(
            "sensor",
            "cpu_temperature",
            "Metixel CPU Temperature",
            {
                "state_topic": f"{prefix}/health",
                "value_template": "{{ value_json.cpu_temp_c | default('unknown') }}",
                "device_class": "temperature",
                "unit_of_measurement": "°C",
                "entity_category": "diagnostic",
                "icon": "mdi:thermometer",
            },
        )
        add(
            "sensor",
            "cpu_usage",
            "Metixel CPU Usage",
            {
                "state_topic": f"{prefix}/health",
                "value_template": "{{ value_json.cpu_percent | default(0) }}",
                "unit_of_measurement": "%",
                "entity_category": "diagnostic",
                "icon": "mdi:cpu-64-bit",
            },
        )
        add(
            "sensor",
            "memory_used",
            "Metixel Memory Used",
            {
                "state_topic": f"{prefix}/health",
                "value_template": "{{ value_json.memory_percent | default(0) }}",
                "unit_of_measurement": "%",
                "entity_category": "diagnostic",
                "icon": "mdi:memory",
            },
        )
        add(
            "sensor",
            "swap_used",
            "Metixel Swap Used",
            {
                "state_topic": f"{prefix}/health",
                "value_template": "{{ value_json.swap_percent | default(0) }}",
                "unit_of_measurement": "%",
                "entity_category": "diagnostic",
                "icon": "mdi:swap-horizontal",
            },
        )
        return out
