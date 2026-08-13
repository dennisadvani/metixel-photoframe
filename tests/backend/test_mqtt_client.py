# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""MQTTClient — control-command routing via a ``FakeMqttGateway``.

The real client talks to the broker through ``paho-mqtt``; the fake gateway
implements the ``MqttGateway`` port so the business logic (topic routing and
command mapping) is tested without a broker.
"""

from __future__ import annotations

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
        self.published.append((topic, payload))

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
    def _make(tmp_path: Path):
        from metixel.backend.mqtt_client import MQTTClient
        from metixel.backend.state import StateManager
        from metixel.shared.config import Config

        config_path = tmp_path / "config.json"
        Config().save(config_path)
        state = StateManager(config_path, tmp_path / "run")
        ipc = FakeIPC()
        mqtt = FakeMqttGateway()
        return MQTTClient(state, ipc, mqtt=mqtt), ipc, mqtt

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

        assert len(mqtt.topics) == 2
        assert all(t.endswith(("/cmd", "/album/set")) for t in mqtt.topics)
