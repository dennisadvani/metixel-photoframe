# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Display-power choke-point — MQTT stays in sync with every source.

The daemon's ``set_display_power()`` is the single place that changes screen
power.  It must: update the daemon flag, send the ``screen_on``/``screen_off``
IPC to the frontend, and publish the new state to MQTT immediately — so Home
Assistant reflects changes made from the Web UI, the display scheduler, the
keyboard/CEC/IR remotes, and MQTT commands alike (no waiting for the 30s
periodic publish).
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest import mock

import pytest


class FakeIPC:
    """IPCClient stand-in that records sent ControlMessages."""

    def __init__(self) -> None:
        self.sent: list = []

    def send(self, msg) -> None:
        self.sent.append(msg)

    def close(self) -> None:
        pass


class FakeMqttClient:
    """MQTTClient stand-in recording immediate screen publishes."""

    def __init__(self) -> None:
        self.publish_count = 0

    def publish_screen_now(self) -> None:
        self.publish_count += 1


def _make_daemon(tmp_path: Path, monkeypatch, mqtt_client=None):
    """Build a real BackendDaemon with a fake IPC + MQTT client."""
    import metixel.backend.daemon as daemon_mod
    from metixel.shared.config import Config

    config_path = tmp_path / "config.json"
    Config().save(config_path)
    # Inject a fake IPCClient so no Pi-only Unix socket is used.
    monkeypatch.setattr(daemon_mod, "IPCClient", FakeIPC)
    monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
    daemon = daemon_mod.BackendDaemon(config_path)
    daemon._mqtt_client = mqtt_client if mqtt_client is not None else FakeMqttClient()
    return daemon


def _freeze_time(monkeypatch, hour: int, minute: int = 0) -> None:
    """Freeze daemon's time.localtime to a fixed time for schedule tests."""
    import metixel.backend.daemon as daemon_mod

    frozen = time.struct_time((2026, 8, 15, hour, minute, 0, 5, 227, 0))
    monkeypatch.setattr(daemon_mod.time, "localtime", lambda: frozen)


class TestDaemonSetDisplayPower:
    """The daemon choke-point must update flag + IPC + publish MQTT."""

    def test_power_on_updates_flag_sends_ipc_and_publishes(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        daemon = _make_daemon(tmp_path, monkeypatch)
        daemon._display_on = False

        daemon.set_display_power(True, source="test")

        assert daemon._display_on is True
        assert daemon._ipc.sent[-1].cmd == "screen_on"
        assert daemon._mqtt_client.publish_count == 1

    def test_power_off_updates_flag_sends_ipc_and_publishes(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        daemon = _make_daemon(tmp_path, monkeypatch)
        daemon._display_on = True

        daemon.set_display_power(False, source="test")

        assert daemon._display_on is False
        assert daemon._ipc.sent[-1].cmd == "screen_off"
        assert daemon._mqtt_client.publish_count == 1

    def test_no_mqtt_client_does_not_crash(self, tmp_path: Path, monkeypatch) -> None:
        daemon = _make_daemon(tmp_path, monkeypatch, mqtt_client=None)

        daemon.set_display_power(False, source="test")

        assert daemon._display_on is False
        assert daemon._ipc.sent[-1].cmd == "screen_off"

    def test_quiet_reassert_resends_ipc_but_does_not_publish_when_unchanged(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The scheduler's quiet retry re-sends the IPC (self-heal a lost
        message) but does not spam MQTT/logs when the state is unchanged."""
        daemon = _make_daemon(tmp_path, monkeypatch)
        daemon._display_on = False  # already off
        sent_before = len(daemon._ipc.sent)

        daemon.set_display_power(False, source="schedule", quiet=True)

        # IPC re-sent so a lost screen_off self-heals…
        assert len(daemon._ipc.sent) == sent_before + 1
        assert daemon._ipc.sent[-1].cmd == "screen_off"
        # …but MQTT is not re-published for an unchanged state.
        assert daemon._mqtt_client.publish_count == 0

    def test_quiet_still_publishes_when_state_changes(self, tmp_path: Path, monkeypatch) -> None:
        """Even in quiet mode, a real state change publishes to MQTT."""
        daemon = _make_daemon(tmp_path, monkeypatch)
        daemon._display_on = True  # was on

        daemon.set_display_power(False, source="schedule", quiet=True)

        assert daemon._display_on is False
        assert daemon._ipc.sent[-1].cmd == "screen_off"
        assert daemon._mqtt_client.publish_count == 1


class TestBootDisplayState:
    """On boot, _display_on must follow the schedule (MQTT starts before the
    scheduler thread), defaulting to on when the schedule is disabled."""

    def test_boot_state_on_when_schedule_disabled(self, tmp_path: Path, monkeypatch) -> None:
        daemon = _make_daemon(tmp_path, monkeypatch)
        assert daemon._display_on is True  # schedule disabled → on

    def test_display_should_be_on_in_window(self, tmp_path: Path, monkeypatch) -> None:
        daemon = _make_daemon(tmp_path, monkeypatch)
        daemon._state.update_config(
            "display",
            {
                "schedule_enabled": True,
                "schedule_on_time": "07:00",
                "schedule_off_time": "22:00",
            },
        )
        _freeze_time(monkeypatch, 12)  # 12:00 → on window
        assert daemon._display_should_be_on() is True

    def test_display_should_be_off_outside_window(self, tmp_path: Path, monkeypatch) -> None:
        daemon = _make_daemon(tmp_path, monkeypatch)
        daemon._state.update_config(
            "display",
            {
                "schedule_enabled": True,
                "schedule_on_time": "07:00",
                "schedule_off_time": "22:00",
            },
        )
        _freeze_time(monkeypatch, 23)  # 23:00 → off window
        assert daemon._display_should_be_on() is False

    def test_wrapped_window_on_overnight(self, tmp_path: Path, monkeypatch) -> None:
        """on > off → the window wraps across midnight (on overnight)."""
        daemon = _make_daemon(tmp_path, monkeypatch)
        daemon._state.update_config(
            "display",
            {
                "schedule_enabled": True,
                "schedule_on_time": "22:00",
                "schedule_off_time": "07:00",
            },
        )
        _freeze_time(monkeypatch, 23)  # 23:00 → on (after 22:00)
        assert daemon._display_should_be_on() is True
        _freeze_time(monkeypatch, 3)  # 03:00 → on (before 07:00)
        assert daemon._display_should_be_on() is True
        _freeze_time(monkeypatch, 12)  # 12:00 → off (outside wrapped window)
        assert daemon._display_should_be_on() is False

    def test_wrapped_brief_off_window(self, tmp_path: Path, monkeypatch) -> None:
        """on=08:50 / off=08:45 → off only during 08:45–08:50 (on otherwise)."""
        daemon = _make_daemon(tmp_path, monkeypatch)
        daemon._state.update_config(
            "display",
            {
                "schedule_enabled": True,
                "schedule_on_time": "08:50",
                "schedule_off_time": "08:45",
            },
        )
        _freeze_time(monkeypatch, 8, 44)  # 08:44 → on (before the off window)
        assert daemon._display_should_be_on() is True
        _freeze_time(monkeypatch, 8, 46)  # 08:46 → off (inside the off window)
        assert daemon._display_should_be_on() is False
        _freeze_time(monkeypatch, 8, 50)  # 08:50 → on (wrapped window resumes)
        assert daemon._display_should_be_on() is True
        _freeze_time(monkeypatch, 12)  # 12:00 → on
        assert daemon._display_should_be_on() is True

    def test_boot_state_tracks_schedule_when_off_window(self, tmp_path: Path, monkeypatch) -> None:
        """The daemon initialises _display_on from the schedule at __init__."""
        import metixel.backend.daemon as daemon_mod
        from metixel.shared.config import Config

        config_path = tmp_path / "config.json"
        Config().save(config_path)
        monkeypatch.setattr(daemon_mod, "IPCClient", FakeIPC)
        monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
        _freeze_time(monkeypatch, 23)
        # Enable the schedule before construction so __init__ sees it.
        from metixel.backend.state import StateManager

        state = StateManager(config_path, run_dir=tmp_path / "run")
        state.update_config(
            "display",
            {
                "schedule_enabled": True,
                "schedule_on_time": "07:00",
                "schedule_off_time": "22:00",
            },
        )
        daemon = daemon_mod.BackendDaemon(config_path)
        assert daemon._display_on is False


class TestMQTTPublishScreenNow:
    """publish_screen_now() must emit metixel/<device_id>/screen from the daemon flag."""

    @staticmethod
    def _make_client(tmp_path: Path, daemon_on: bool):
        from metixel.backend.mqtt_client import MQTTClient
        from metixel.backend.state import StateManager
        from metixel.shared.config import Config

        class _Daemon:
            _display_on = daemon_on

        config_path = tmp_path / "config.json"
        Config().save(config_path)
        state = StateManager(config_path, tmp_path / "run")
        # Deterministic device id so the scoped topic is stable in tests.
        state.update_config("mqtt", {"device_id": "testframe"})
        mqtt = _FakeGateway()
        return MQTTClient(state, FakeIPC(), mqtt=mqtt, daemon=_Daemon()), mqtt

    def test_publishes_on_when_daemon_on(self, tmp_path: Path) -> None:
        client, mqtt = self._make_client(tmp_path, daemon_on=True)
        client.publish_screen_now()
        assert mqtt.published[-1] == ("metixel/testframe/screen", "ON", False)

    def test_publishes_off_when_daemon_off(self, tmp_path: Path) -> None:
        client, mqtt = self._make_client(tmp_path, daemon_on=False)
        client.publish_screen_now()
        assert mqtt.published[-1] == ("metixel/testframe/screen", "OFF", False)


class _FakeGateway:
    """Implements the MqttGateway port surface used by the MQTT client."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool]] = []

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
        pass

    def set_credentials(self, username: str, password: str) -> None:
        pass

    def set_will(self, topic: str, payload: str, *, retain: bool = False) -> None:
        pass

    def set_handlers(self, on_connect, on_message) -> None:
        pass


class TestInputHandlerScreenRouting:
    """Keyboard/CEC/IR must route screen commands through the daemon
    choke-point when a display_power callback is wired."""

    def test_keyboard_routes_screen_on_through_callback(self) -> None:
        from metixel.backend.input_handlers.keyboard import KeyboardHandler

        calls: list[bool] = []

        def display_power(on: bool) -> None:
            calls.append(on)

        handler = KeyboardHandler(
            config={"keyboard_map": {}}, ipc=FakeIPC(), display_power=display_power
        )
        handler._dispatch("screen_on")
        assert calls == [True]

    def test_keyboard_routes_screen_off_through_callback(self) -> None:
        from metixel.backend.input_handlers.keyboard import KeyboardHandler

        calls: list[bool] = []

        def display_power(on: bool) -> None:
            calls.append(on)

        handler = KeyboardHandler(
            config={"keyboard_map": {}}, ipc=FakeIPC(), display_power=display_power
        )
        handler._dispatch("screen_off")
        assert calls == [False]

    def test_keyboard_falls_back_to_ipc_without_callback(self) -> None:
        from metixel.backend.input_handlers.keyboard import KeyboardHandler

        ipc = FakeIPC()
        handler = KeyboardHandler(config={"keyboard_map": {}}, ipc=ipc, display_power=None)
        handler._dispatch("screen_off")
        assert ipc.sent[-1].cmd == "screen_off"

    def test_keyboard_non_screen_goes_via_ipc(self) -> None:
        from metixel.backend.input_handlers.keyboard import KeyboardHandler

        calls: list[bool] = []

        def display_power(on: bool) -> None:
            calls.append(on)

        ipc = FakeIPC()
        handler = KeyboardHandler(config={"keyboard_map": {}}, ipc=ipc, display_power=display_power)
        handler._dispatch("next")
        assert calls == []
        assert ipc.sent[-1].cmd == "next"

    def test_cec_routes_screen_through_callback(self, tmp_path: Path) -> None:
        from metixel.backend.input_handlers.cec import CECHandler
        from metixel.backend.state import StateManager
        from metixel.shared.config import Config

        calls: list[bool] = []

        def display_power(on: bool) -> None:
            calls.append(on)

        config_path = tmp_path / "config.json"
        Config().save(config_path)
        state = StateManager(config_path, tmp_path / "run")
        ipc = FakeIPC()
        handler = CECHandler(state, ipc, cec=None, display_power=display_power)
        handler._cec_key_callback(0x42, 0)  # Screen Off
        assert calls == [False]
        assert ipc.sent == []

    def test_ir_routes_screen_through_callback(self, tmp_path: Path) -> None:
        from metixel.backend.input_handlers.ir import IRHandler
        from metixel.backend.state import StateManager
        from metixel.shared.config import Config

        calls: list[bool] = []

        def display_power(on: bool) -> None:
            calls.append(on)

        config_path = tmp_path / "config.json"
        Config().save(config_path)
        state = StateManager(config_path, tmp_path / "run")
        ipc = FakeIPC()
        handler = IRHandler(state, ipc, ir=None, display_power=display_power)
        handler._process_line("0000000000f40bf0 00 KEY_POWER lircd.conf")
        assert calls == [True]
        assert ipc.sent == []


@pytest.fixture
def mock_state(tmp_path: Path):
    """A real StateManager backed by a temp config file."""
    from metixel.backend.state import StateManager

    config_path = tmp_path / "config.json"
    return StateManager(config_path, run_dir=tmp_path / "run")


@pytest.fixture
def mock_ipc():
    """MagicMock stand-in for the IPC client (frontend command channel)."""
    return mock.MagicMock()


@pytest.fixture
def mock_update_manager():
    """MagicMock stand-in for the OTA UpdateManager."""
    return mock.MagicMock()


@pytest.fixture
def app(mock_state, mock_ipc, mock_update_manager):
    """A real Flask app wired with mocked outbound dependencies."""
    from metixel.backend.web.server import create_app

    return create_app(
        mock_state,
        mock_ipc,
        opt_queue=None,
        update_mgr=mock_update_manager,
        daemon=None,
    )


class TestControlRouteScreenRouting:
    """POST /api/control with screen_on/screen_off must go through the
    daemon choke-point (which publishes MQTT), not a bare IPC send."""

    def test_screen_off_routes_through_daemon(self, app) -> None:
        calls: list[tuple[bool, str]] = []

        class _FakeDaemon:
            def set_display_power(self, on: bool, source: str = "") -> None:
                calls.append((on, source))

        app.config["METIXEL_DAEMON"] = _FakeDaemon()
        resp = app.test_client().post("/api/control", json={"cmd": "screen_off"})
        assert resp.status_code == 200
        assert calls == [(False, "web")]

    def test_screen_on_routes_through_daemon(self, app) -> None:
        calls: list[tuple[bool, str]] = []

        class _FakeDaemon:
            def set_display_power(self, on: bool, source: str = "") -> None:
                calls.append((on, source))

        app.config["METIXEL_DAEMON"] = _FakeDaemon()
        resp = app.test_client().post("/api/control", json={"cmd": "screen_on"})
        assert resp.status_code == 200
        assert calls == [(True, "web")]

    def test_screen_falls_back_to_ipc_without_daemon(self, app, mock_ipc) -> None:
        app.config["METIXEL_DAEMON"] = None
        resp = app.test_client().post("/api/control", json={"cmd": "screen_off"})
        assert resp.status_code == 200
        # Without a daemon, the command is still sent to the frontend via IPC.
        assert mock_ipc.send.called

    def test_non_screen_commands_do_not_touch_display(self, app, mock_ipc) -> None:
        app.config["METIXEL_DAEMON"] = None
        resp = app.test_client().post("/api/control", json={"cmd": "next"})
        assert resp.status_code == 200
        assert mock_ipc.send.called
