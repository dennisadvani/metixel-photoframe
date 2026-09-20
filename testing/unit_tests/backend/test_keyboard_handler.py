# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""KeyboardHandler — default key mapping + dispatch.

The real handler reads key events from ``evdev`` devices (USB remotes /
mini-keyboards) and translates Linux key codes into Metixel control commands,
dispatching them to the frontend over IPC.

These tests pin the **default key map** so a regression in the out-of-the-box
mapping is caught without any hardware: key code 105 (KEY_LEFT) must dispatch
``prev``, 106 (KEY_RIGHT) must dispatch ``next``, and 28 (KEY_ENTER) must
dispatch ``toggle_pause``.  A fake IPC client records the dispatched commands;
no real keyboard or guest device is used.
"""

from __future__ import annotations

import metixel.backend.input_handlers.keyboard as kb
from metixel.shared.ipc import ControlMessage


class FakeIPC:
    """Satisfies the ``IPCSender`` port (see metixel.shared.ipc)."""

    def __init__(self) -> None:
        self.sent: list[ControlMessage] = []

    def send(self, msg: ControlMessage) -> bool:
        self.sent.append(msg)
        return True

    def close(self) -> None:
        pass


def _handler():
    """Build a KeyboardHandler with no stored key map (pure defaults)."""
    ipc = FakeIPC()
    handler = kb.KeyboardHandler(config={"keyboard_map": {}}, ipc=ipc)
    return handler, ipc


# Linux key codes for the default map (see DEFAULT_KEY_MAP).
KEY_LEFT = 105
KEY_RIGHT = 106
KEY_ENTER = 28


class TestDefaultKeyMap:
    """The out-of-the-box DEFAULT_KEY_MAP must map the expected keys."""

    def test_default_map_contains_expected_entries(self) -> None:
        assert kb.DEFAULT_KEY_MAP[KEY_LEFT] == "prev"
        assert kb.DEFAULT_KEY_MAP[KEY_RIGHT] == "next"
        assert kb.DEFAULT_KEY_MAP[KEY_ENTER] == "toggle_pause"

    def test_default_map_has_only_the_three_defaults(self) -> None:
        assert set(kb.DEFAULT_KEY_MAP.items()) == {
            (KEY_LEFT, "prev"),
            (KEY_RIGHT, "next"),
            (KEY_ENTER, "toggle_pause"),
        }

    def test_key_map_property_matches_defaults(self) -> None:
        handler, _ = _handler()
        # {cmd: [codes]}
        assert handler.key_map == {
            "prev": [KEY_LEFT],
            "next": [KEY_RIGHT],
            "toggle_pause": [KEY_ENTER],
        }


class TestDefaultKeyDispatch:
    """Pressing the default keys must dispatch the expected IPC command."""

    def test_left_dispatches_prev(self) -> None:
        handler, ipc = _handler()
        handler._dispatch(handler._key_map[KEY_LEFT])
        assert [m.cmd for m in ipc.sent] == ["prev"]

    def test_right_dispatches_next(self) -> None:
        handler, ipc = _handler()
        handler._dispatch(handler._key_map[KEY_RIGHT])
        assert [m.cmd for m in ipc.sent] == ["next"]

    def test_enter_dispatches_toggle_pause(self) -> None:
        handler, ipc = _handler()
        handler._dispatch(handler._key_map[KEY_ENTER])
        assert [m.cmd for m in ipc.sent] == ["toggle_pause"]

    def test_each_key_is_a_control_message(self) -> None:
        handler, ipc = _handler()
        for code in (KEY_LEFT, KEY_RIGHT, KEY_ENTER):
            handler._dispatch(handler._key_map[code])
        assert [m.cmd for m in ipc.sent] == ["prev", "next", "toggle_pause"]
        # Each is a real ControlMessage (JSON-serialisable).
        for msg in ipc.sent:
            assert isinstance(msg.to_json(), str)


class TestConfigOverride:
    """How stored config interacts with the defaults.

    ``__init__`` (what a freshly restarted process loads from disk) and
    ``set_key_map`` (what the web Learn/Clear routes invoke) share ONE
    semantics: start from the defaults, then for every command in the stored
    map REPLACE that command's keys with the stored codes — an empty list
    clears the command (its default keys included).  They used to diverge
    (``__init__`` merged additively), so a key cleared or re-learned in the UI
    came back after a reboot.
    """

    def test_init_replaces_command_keys_like_set_key_map(self) -> None:
        # Mapping KEY_LEFT to "next" REPLACES next's default (106) and, since
        # 105 was prev's default, prev no longer has a key.
        handler = kb.KeyboardHandler(config={"keyboard_map": {"next": [KEY_LEFT]}}, ipc=FakeIPC())
        assert handler.key_map["next"] == [KEY_LEFT], handler.key_map
        assert handler.key_map.get("prev") is None
        assert handler.key_map["toggle_pause"] == [KEY_ENTER]

    def test_init_empty_list_clears_default(self) -> None:
        # A cleared command stays cleared after a restart.
        handler = kb.KeyboardHandler(config={"keyboard_map": {"prev": []}}, ipc=FakeIPC())
        assert handler.key_map.get("prev") is None
        assert handler.key_map["next"] == [KEY_RIGHT]

    def test_init_and_set_key_map_agree(self) -> None:
        stored = {"pause": [57], "prev": [], "next": [108, KEY_RIGHT]}
        from_init = kb.KeyboardHandler(config={"keyboard_map": stored}, ipc=FakeIPC())
        via_set, _ = _handler()
        via_set.set_key_map(stored)
        assert from_init._key_map == via_set._key_map

    def test_init_ignores_unknown_commands_and_bad_codes(self) -> None:
        handler = kb.KeyboardHandler(
            config={"keyboard_map": {"bogus": [1], "pause": ["x", 57]}}, ipc=FakeIPC()
        )
        assert handler.key_map.get("bogus") is None
        assert handler.key_map["pause"] == [57]

    def test_set_key_map_replaces_command_keys(self) -> None:
        # set_key_map (the Learn path) replaces: mapping KEY_ENTER to pause
        # removes the default toggle_pause (28) binding.
        handler, _ = _handler()
        handler.set_key_map({"pause": [KEY_ENTER]})
        assert handler.key_map["pause"] == [KEY_ENTER]
        assert handler.key_map.get("toggle_pause") is None

    def test_set_key_map_empty_clears_command(self) -> None:
        handler, _ = _handler()
        handler.set_key_map({"prev": []})
        assert handler.key_map.get("prev") is None

    def test_learn_re_maps_a_key_and_updates_dispatch(self) -> None:
        handler, ipc = _handler()
        # Re-map KEY_ENTER (28) from toggle_pause to pause via set_key_map.
        handler.set_key_map({"pause": [KEY_ENTER]})
        handler._dispatch(handler._key_map[KEY_ENTER])
        assert [m.cmd for m in ipc.sent] == ["pause"]


class TestCustomKeys:
    """Additional / non-default key mappings (pause, resume, screen, album).

    Beyond the three defaults, a user can map extra Linux key codes to any
    VALID_COMMAND via the dashboard Learn UI.  These tests pin several common
    extra bindings so custom mappings keep working.
    """

    # Extra Linux key codes used for custom mappings.
    KEY_SPACE = 57
    KEY_P = 25
    KEY_A = 30
    KEY_DOWN = 108
    KEY_F1 = 59

    def test_custom_keys_are_valid_commands(self) -> None:
        # Every command a user can map is in VALID_COMMANDS.
        for cmd in ("pause", "resume", "screen_on", "screen_off", "switch_album"):
            assert cmd in kb.VALID_COMMANDS

    def test_pause_and_resume_map_and_dispatch(self) -> None:
        handler, ipc = _handler()
        handler.set_key_map(
            {
                "pause": [self.KEY_SPACE],
                "resume": [self.KEY_P],
            }
        )
        handler._dispatch(handler._key_map[self.KEY_SPACE])
        handler._dispatch(handler._key_map[self.KEY_P])
        assert [m.cmd for m in ipc.sent] == ["pause", "resume"]

    def test_screen_commands_route_through_display_power(self) -> None:
        # screen_on / screen_off must go through the display_power callback,
        # not IPC (so the daemon flag + MQTT stay in sync).
        calls: list[bool] = []
        handler = kb.KeyboardHandler(
            config={"keyboard_map": {}},
            ipc=FakeIPC(),
            display_power=lambda on: calls.append(on),
        )
        handler.set_key_map(
            {
                "screen_on": [self.KEY_A],
                "screen_off": [self.KEY_DOWN],
            }
        )
        handler._dispatch(handler._key_map[self.KEY_A])
        handler._dispatch(handler._key_map[self.KEY_DOWN])
        assert calls == [True, False]
        # No IPC sent for screen commands.
        assert isinstance(handler._ipc, FakeIPC)
        assert len(handler._ipc.sent) == 0

    def test_switch_album_dispatches(self) -> None:
        handler, ipc = _handler()
        handler.set_key_map({"switch_album": [self.KEY_F1]})
        handler._dispatch(handler._key_map[self.KEY_F1])
        assert [m.cmd for m in ipc.sent] == ["switch_album"]

    def test_unknown_command_ignored_by_set_key_map(self) -> None:
        # set_key_map silently skips commands not in VALID_COMMANDS.
        handler, _ = _handler()
        handler.set_key_map({"not_a_real_cmd": [self.KEY_F1]})
        assert handler.key_map.get("not_a_real_cmd") is None
        # Defaults are untouched.
        assert handler.key_map["next"] == [KEY_RIGHT]

    def test_extra_key_does_not_overwrite_defaults(self) -> None:
        # Mapping a brand-new code (not colliding with defaults) leaves the
        # three default bindings intact.
        handler, _ = _handler()
        handler.set_key_map({"pause": [self.KEY_SPACE]})
        assert handler.key_map["pause"] == [self.KEY_SPACE]
        assert handler.key_map["next"] == [KEY_RIGHT]
        assert handler.key_map["prev"] == [KEY_LEFT]
        assert handler.key_map["toggle_pause"] == [KEY_ENTER]


# ── Device hot-unplug handling (fake evdev + fake selector) ──────────────────


class _FakeDevice:
    def __init__(self, path: str, fd: int, events=None, error: OSError | None = None) -> None:
        self.path = path
        self.fd = fd
        self.name = f"dev-{fd}"
        self.closed = False
        self._events = list(events or [])
        self._error = error

    def capabilities(self):
        return {1: [28]}  # EV_KEY

    def read(self):
        if self._error is not None:
            raise self._error
        yield from self._events

    def close(self) -> None:
        self.closed = True


class _FakeSelector:
    """Reports every registered fd as ready on each select() call and lets the
    test decide when the handler should stop."""

    def __init__(self, on_select) -> None:
        self.registered: list[int] = []  # history
        self.unregistered: list[int] = []  # history
        self.live: list[int] = []
        self._on_select = on_select
        self.selects = 0

    def register(self, fd, events):
        self.registered.append(fd)
        self.live.append(fd)

    def unregister(self, fd):
        self.unregistered.append(fd)
        self.live.remove(fd)

    def select(self, timeout=None):
        self.selects += 1
        self._on_select(self.selects)
        from types import SimpleNamespace

        return [(SimpleNamespace(fd=fd, fileobj=fd), 1) for fd in list(self.live)]

    def close(self) -> None:
        pass


def _install_fake_evdev(monkeypatch, devices_by_path: dict[str, _FakeDevice]) -> None:
    import sys
    from types import SimpleNamespace

    def list_devices():
        return list(devices_by_path)

    def InputDevice(path):  # noqa: N802 — mirrors evdev's class name
        return devices_by_path[path]

    fake = SimpleNamespace(
        list_devices=list_devices,
        InputDevice=InputDevice,
        ecodes=SimpleNamespace(EV_KEY=1, KEY={106: "KEY_RIGHT"}),
    )
    monkeypatch.setitem(sys.modules, "evdev", fake)


class TestDeviceUnplug:
    def test_read_oserror_unregisters_closes_and_forgets_device(self, monkeypatch, caplog) -> None:
        import errno
        import logging

        handler, ipc = _handler()
        dead = _FakeDevice("/dev/input/event0", fd=10, error=OSError(errno.ENODEV, "unplugged"))
        devices = {"/dev/input/event0": dead}
        _install_fake_evdev(monkeypatch, devices)
        monkeypatch.setattr(kb.KeyboardHandler, "_RESCAN_INTERVAL", 10_000.0)

        def on_select(n):
            if n >= 2:
                handler.stop()

        sel = _FakeSelector(on_select)
        monkeypatch.setattr(kb.selectors, "DefaultSelector", lambda: sel)

        with caplog.at_level(logging.INFO, logger="metixel.backend.input_handlers.keyboard"):
            handler.run()

        assert sel.unregistered.count(10) == 1
        assert dead.closed is True
        assert "Keyboard device disconnected" in caplog.text
        assert ipc.sent == []

    def test_replugged_device_is_picked_up_by_rescan(self, monkeypatch) -> None:
        import errno
        from types import SimpleNamespace

        handler, ipc = _handler()
        dead = _FakeDevice("/dev/input/event0", fd=10, error=OSError(errno.ENODEV, "unplugged"))
        key_right = SimpleNamespace(type=1, code=106, value=1)
        alive = _FakeDevice("/dev/input/event0", fd=11, events=[key_right])
        devices = {"/dev/input/event0": dead}
        _install_fake_evdev(monkeypatch, devices)
        monkeypatch.setattr(kb.KeyboardHandler, "_RESCAN_INTERVAL", 0.0)

        def on_select(n):
            if n == 1:
                del devices["/dev/input/event0"]  # unplug: node gone, fd 10 errors
            if n == 2:
                devices["/dev/input/event0"] = alive  # replug: same path, new fd
            if n >= 4:
                handler.stop()

        sel = _FakeSelector(on_select)
        monkeypatch.setattr(kb.selectors, "DefaultSelector", lambda: sel)

        handler.run()

        assert 10 in sel.unregistered
        assert 11 in sel.registered, "rescan must register the replugged device"
        assert ipc.sent and all(m.cmd == "next" for m in ipc.sent)

    def test_blocking_io_error_keeps_device(self, monkeypatch) -> None:
        handler, _ = _handler()
        busy = _FakeDevice("/dev/input/event0", fd=12, error=BlockingIOError())
        _install_fake_evdev(monkeypatch, {"/dev/input/event0": busy})
        monkeypatch.setattr(kb.KeyboardHandler, "_RESCAN_INTERVAL", 10_000.0)

        def on_select(n):
            if n >= 2:
                handler.stop()

        sel = _FakeSelector(on_select)
        monkeypatch.setattr(kb.selectors, "DefaultSelector", lambda: sel)
        handler.run()
        # Only the final teardown unregisters it — not the BlockingIOError.
        assert sel.unregistered.count(12) == 1
        assert sel.selects >= 2
