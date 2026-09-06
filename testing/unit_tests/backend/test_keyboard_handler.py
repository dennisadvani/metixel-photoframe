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


class FakeIPC:
    """IPCClient stand-in that records sent ControlMessages."""

    def __init__(self) -> None:
        self.sent: list = []

    def send(self, msg) -> None:
        self.sent.append(msg)

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

    Two distinct paths:
    - ``__init__`` merges stored keys ADDITIVELY over the defaults (defaults
      are retained and config codes are added on top).  This is what a freshly
      restarted process loads from disk.
    - ``set_key_map`` (used by the web Learn/clear routes) starts fresh from
      the defaults and REPLACES a command's keys with the provided codes
      (empty = clear).  This is what the UI actually invokes.
    """

    def test_init_merges_stored_additively_over_defaults(self) -> None:
        # __init__ is additive: mapping KEY_LEFT to "next" ADDS it to next but
        # keeps the default next (106) too; the default prev (105) binding is
        # overwritten because that code was re-mapped.
        handler = kb.KeyboardHandler(
            config={"keyboard_map": {"next": [KEY_LEFT]}}, ipc=FakeIPC()
        )
        assert handler.key_map["next"] == [KEY_LEFT, KEY_RIGHT], handler.key_map
        # 105 is now "next", so the default "prev" command no longer has a key.
        assert handler.key_map.get("prev") is None

    def test_init_empty_list_leaves_default_intact(self) -> None:
        # __init__ never removes defaults, so an empty list has no effect here.
        handler = kb.KeyboardHandler(
            config={"keyboard_map": {"prev": []}}, ipc=FakeIPC()
        )
        assert handler.key_map["prev"] == [KEY_LEFT]

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
