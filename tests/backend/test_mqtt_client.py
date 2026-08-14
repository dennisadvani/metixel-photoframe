# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""MQTTClient — control-command routing via a ``FakeMqttGateway``.

The real client talks to the broker through ``paho-mqtt``; the fake gateway
implements the ``MqttGateway`` port so the business logic (topic routing and
command mapping) is tested without a broker.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class FakeIPC:
    def __init__(self) -> None:
        self.sent = []

    def send(self, msg) -> None:
        self.sent.append(msg)


class FakeMsg:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class FakeMqttGateway:
    """Implements the full ``MqttGateway`` port surface."""

    def __init__(self) -> None:
        self.topics: list[str] = []
        self.published: list[tuple[str, str]] = []
        self.credentials: tuple[str, str] | None = None

    def connect(self, host: str, port: int, *, keepalive: int = 60) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def loop_start(self) -> None:
        pass

    def loop_stop(self) -> None:
        pass

    def publish(self, topic: str, payload: str, *, retain: bool = False) -> None:
        self.published.append((topic, payload, retain))

    def subscribe(self, topic: str) -> None:
        self.topics.append(topic)

    def set_credentials(self, username: str, password: str) -> None:
        self.credentials = (username, password)

    def set_will(self, topic: str, payload: str, *, retain: bool = False) -> None:
        pass

    def set_handlers(self, on_connect: Any, on_message: Any) -> None:
        pass


class TestMQTTClient:
    @staticmethod
    def _make(tmp_path: Path, daemon: Any | None = None):
        from metixel.backend.mqtt_client import MQTTClient
        from metixel.backend.state import StateManager
        from metixel.shared.config import Config

        config_path = tmp_path / "config.json"
        Config().save(config_path)
        state = StateManager(config_path, tmp_path / "run")
        # Deterministic device id so discovery object/unique ids are stable.
        state.update_config("mqtt", {"device_id": "testframe"})
        ipc = FakeIPC()
        mqtt = FakeMqttGateway()
        return MQTTClient(state, ipc, mqtt=mqtt, daemon=daemon), ipc, mqtt

    def test_handle_cmd_next(self, tmp_path: Path) -> None:
        client, ipc, _ = self._make(tmp_path)
        client._handle_cmd("next")
        assert ipc.sent[-1].cmd == "next"

    def test_handle_cmd_pause_case_insensitive(self, tmp_path: Path) -> None:
        client, ipc, _ = self._make(tmp_path)
        client._handle_cmd("PAUSE")
        assert ipc.sent[-1].cmd == "pause"

    def test_handle_cmd_unknown_ignored(self, tmp_path: Path) -> None:
        client, ipc, _ = self._make(tmp_path)
        client._handle_cmd("unknown_command")
        assert ipc.sent == []

    def test_on_message_cmd_topic(self, tmp_path: Path) -> None:
        client, ipc, _ = self._make(tmp_path)
        client._on_message(None, None, FakeMsg("metixel/cmd", b"pause"))
        assert ipc.sent[-1].cmd == "pause"

    def test_on_message_album_set_topic(self, tmp_path: Path) -> None:
        client, ipc, _ = self._make(tmp_path)
        client._on_message(None, None, FakeMsg("metixel/album/set", b"album-1"))
        assert ipc.sent[-1].cmd == "switch_album"
        assert ipc.sent[-1].args == {"album_id": "album-1"}

    def test_on_connect_subscribes_control_topics(self, tmp_path: Path) -> None:
        from metixel.shared.ports import MqttGateway

        client, _ipc, mqtt = self._make(tmp_path)
        assert isinstance(mqtt, MqttGateway)

        client._on_connect(None, None, None, 0)

        assert len(mqtt.topics) == 3
        assert all(t.endswith(("/cmd", "/album/set", "/screen/set")) for t in mqtt.topics)


class _FakeDaemon:
    """Minimal stand-in exposing the daemon's display-state flag."""

    def __init__(self) -> None:
        self._display_on = True


class TestMQTTDiscovery:
    """Home Assistant MQTT Discovery behaviour."""

    @staticmethod
    def _make(tmp_path: Path, daemon: Any | None = None):
        return TestMQTTClient._make(tmp_path, daemon=daemon)

    @staticmethod
    def _discovery_payloads(client, mqtt) -> dict[str, dict]:
        client._publish_discovery("metixel")
        return {
            topic: __import__("json").loads(payload)
            for topic, payload, _retain in mqtt.published
            if topic.startswith("homeassistant/")
        }

    def test_publish_discovery_retained_and_valid(self, tmp_path: Path) -> None:
        client, _ipc, mqtt = self._make(tmp_path)

        client._publish_discovery("metixel")

        # All discovery topics must be retained.
        discovery = [p for p in mqtt.published if p[0].startswith("homeassistant/")]
        assert len(discovery) > 0
        assert all(retain for _, _p, retain in discovery)

        payload = json.loads(discovery[0][1])
        # Valid HA schema: stable unique_id, shared device + availability.
        assert payload["unique_id"].startswith("metixel_testframe_")
        assert payload["availability_topic"] == "metixel/status"
        assert payload["payload_available"] == "online"
        assert payload["payload_not_available"] == "offline"
        assert payload["device"]["identifiers"][0] == "metixel_testframe"
        assert payload["device"]["name"] == "Metixel Photo Frame"
        assert payload["device"]["manufacturer"] == "Metixel"

    def test_discovery_entities_have_required_topics(self, tmp_path: Path) -> None:
        client, _ipc, mqtt = self._make(tmp_path)
        configs = self._discovery_payloads(client, mqtt)

        # Buttons need a command topic + press payload.
        for entity in ("next", "prev", "pause_toggle"):
            cfg = configs[f"homeassistant/button/metixel_testframe_{entity}/config"]
            assert cfg["command_topic"] == "metixel/cmd"
            assert cfg["payload_press"]

        # Screen power switch: command + state topics + payloads.
        switch = configs["homeassistant/switch/metixel_testframe_screen_power/config"]
        assert switch["command_topic"] == "metixel/screen/set"
        assert switch["state_topic"] == "metixel/screen"
        assert switch["payload_on"] == "ON"
        assert switch["payload_off"] == "OFF"

        # Sensors need a state topic and live in the diagnostic category.
        for entity in (
            "current_media",
            "playback_state",
            "uptime",
            "cpu_temperature",
            "memory_used",
            "swap_used",
            "disk_used",
        ):
            cfg = configs[f"homeassistant/sensor/metixel_testframe_{entity}/config"]
            assert cfg["state_topic"]
            assert cfg["entity_category"] == "diagnostic"

        # The current-file-name sensor is disabled unless the user opts in.
        media = configs["homeassistant/sensor/metixel_testframe_current_media/config"]
        assert media["enabled_by_default"] is False

        # Uptime is human-readable — no device_class/unit that implies seconds.
        uptime = configs["homeassistant/sensor/metixel_testframe_uptime/config"]
        assert "{{ d }}d" in uptime["value_template"]
        assert "device_class" not in uptime
        assert "unit_of_measurement" not in uptime

        # Disk used reports a percentage of the root filesystem.
        disk = configs["homeassistant/sensor/metixel_testframe_disk_used/config"]
        assert disk["value_template"] == "{{ value_json.disk_used_percent | default(0) }}"
        assert disk["unit_of_measurement"] == "%"

        # Memory and swap usage are reported as percentages, not GB.
        for entity, field in (("memory_used", "memory_percent"), ("swap_used", "swap_percent")):
            cfg = configs[f"homeassistant/sensor/metixel_testframe_{entity}/config"]
            assert cfg["value_template"] == f"{{{{ value_json.{field} | default(0) }}}}"
            assert cfg["unit_of_measurement"] == "%"
            assert "device_class" not in cfg

        # No `select` entity — the playback dropdown was removed because a
        # momentary command has no state to report (HA would show it as
        # "unknown").  The button entities cover next/prev/pause.
        assert not any("select/" in topic for topic in configs)

    def test_publish_discovery_disabled_publishes_nothing(self, tmp_path: Path) -> None:
        client, _ipc, mqtt = self._make(tmp_path)
        client._state.update_config("mqtt", {"discovery_enabled": False})

        client._publish_discovery("metixel")

        assert not any(t.startswith("homeassistant/") for t, _p, _r in mqtt.published)

    def test_two_frames_have_disjoint_unique_ids(self, tmp_path: Path) -> None:
        """Different device ids → no entity/device collisions in HA."""
        client_a, _ipc_a, mqtt_a = self._make(tmp_path)
        client_a._state.update_config("mqtt", {"device_id": "frame_a"})
        client_b, _ipc_b, mqtt_b = self._make(tmp_path)
        client_b._state.update_config("mqtt", {"device_id": "frame_b"})

        client_a._publish_discovery("metixel")
        client_b._publish_discovery("metixel")

        ids_a = {
            json.loads(p)["unique_id"]
            for t, p, _r in mqtt_a.published
            if t.startswith("homeassistant/")
        }
        ids_b = {
            json.loads(p)["unique_id"]
            for t, p, _r in mqtt_b.published
            if t.startswith("homeassistant/")
        }
        assert ids_a and ids_b
        assert ids_a.isdisjoint(ids_b)

        # Device identifiers differ too.
        def _first_device(mqtt) -> dict:
            for t, p, _r in mqtt.published:
                if t.startswith("homeassistant/"):
                    return json.loads(p)["device"]
            return {}

        assert _first_device(mqtt_a)["identifiers"] != _first_device(mqtt_b)["identifiers"]

    def test_on_connect_publishes_discovery_screen_and_media(self, tmp_path: Path) -> None:
        client, _ipc, mqtt = self._make(tmp_path)

        client._on_connect(None, None, None, 0)

        assert client._connected is True
        assert any(t.startswith("homeassistant/") for t, _p, _r in mqtt.published)
        assert any(t == "metixel/screen" for t, _p, _r in mqtt.published)
        assert any(t == "metixel/state" for t, _p, _r in mqtt.published)
        assert any(t == "metixel/current_media" for t, _p, _r in mqtt.published)

    def test_on_connect_rejection_publishes_nothing(self, tmp_path: Path) -> None:
        """A rejected CONNACK (reason_code != 0) must not publish anything."""
        client, _ipc, mqtt = self._make(tmp_path)

        client._on_connect(None, None, None, 5)  # not authorized

        assert client._connected is False
        assert mqtt.published == []
        assert mqtt.topics == []

    def test_screen_set_topic_routes_power_commands(self, tmp_path: Path) -> None:
        client, ipc, _ = self._make(tmp_path)

        client._on_message(None, None, FakeMsg("metixel/screen/set", b"OFF"))
        assert ipc.sent[-1].cmd == "screen_off"

        client._on_message(None, None, FakeMsg("metixel/screen/set", b"ON"))
        assert ipc.sent[-1].cmd == "screen_on"

    def test_screen_command_updates_daemon_state_and_publishes(self, tmp_path: Path) -> None:
        daemon = _FakeDaemon()
        client, _ipc, mqtt = self._make(tmp_path, daemon=daemon)

        assert client._display_on() is True
        client._handle_cmd("power_off")
        assert client._display_on() is False
        assert daemon._display_on is False
        # New state published immediately so HA's switch updates fast.
        assert mqtt.published[-1] == ("metixel/screen", "OFF", False)

        client._handle_cmd("power_on")
        assert client._display_on() is True
        assert mqtt.published[-1] == ("metixel/screen", "ON", False)

    def test_handle_cmd_toggle_pause(self, tmp_path: Path) -> None:
        client, ipc, _ = self._make(tmp_path)
        client._handle_cmd("toggle_pause")
        assert ipc.sent[-1].cmd == "toggle_pause"

    def test_current_media_data_derives_title_and_state(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import metixel.backend.mqtt_client as mqtt_mod

        state_file = tmp_path / "current_media.json"
        state_file.write_text(json.dumps({"file": "/tmp/photo.jpg", "paused": False}))
        monkeypatch.setattr(mqtt_mod, "_CURRENT_MEDIA_FILE", str(state_file))

        client, _ipc, _ = self._make(tmp_path)
        data = client._current_media_data()

        assert data["title"] == "photo.jpg"
        assert data["state"] == "playing"

    def test_publish_media_publishes_state_and_media(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import metixel.backend.mqtt_client as mqtt_mod

        # Point at a non-existent state file so state is deterministically "off".
        monkeypatch.setattr(mqtt_mod, "_CURRENT_MEDIA_FILE", str(tmp_path / "missing.json"))
        client, _ipc, mqtt = self._make(tmp_path)

        client._publish_media("metixel")

        published = {t: p for t, p, _r in mqtt.published}
        assert "metixel/current_media" in published
        assert published["metixel/state"] == "off"  # no media file → off
