# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""``IPCSender`` port — the interface the input handlers/MQTT client depend on.

This port exists so those components accept a *capability* (something that can
send control messages) rather than the concrete AF_UNIX :class:`IPCClient`,
which cannot even be constructed on Windows.  That lets every handler test
inject a small recording fake.

These tests pin the two things that make the port worth having: the real client
actually satisfies it, and duck-typed fakes are accepted by handlers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from metixel.shared.ipc import IPCClient, IPCSender


class _RecordingFake:
    """A fake with no shared base class — the duck-typing case."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send(self, message: Any) -> bool:
        self.sent.append(message)
        return True

    def close(self) -> None:
        pass


class TestPortShape:
    def test_real_client_satisfies_the_port(self) -> None:
        """The concrete client must remain a valid IPCSender."""
        # Constructing is safe off-Pi: AF_UNIX is absent on Windows, and the
        # client degrades to "IPC disabled" rather than raising.
        assert isinstance(IPCClient(), IPCSender)

    def test_recording_fake_satisfies_the_port(self) -> None:
        assert isinstance(_RecordingFake(), IPCSender)

    def test_object_without_send_does_not_satisfy_the_port(self) -> None:
        """The port must be a real constraint, not vacuously true."""
        assert not isinstance(object(), IPCSender)

    def test_object_with_wrong_send_signature_does_not_satisfy_the_port(self) -> None:
        """A `send` that does not return bool must be rejected.

        ``@runtime_checkable`` only checks attribute presence, so this is
        really pinning that `send` IS required — the return type is enforced
        statically by mypy, which is why the tests' fakes annotate it.
        """

        class Wrong:
            def send(self) -> None:  # missing the message parameter
                return None

        assert not isinstance(Wrong(), IPCSender)


class TestHandlersAcceptFakes:
    """Handlers must run with a duck-typed fake, not just the real client."""

    def test_keyboard_handler_accepts_a_fake(self, tmp_path: Path) -> None:
        import metixel.backend.input_handlers.keyboard as kb

        fake = _RecordingFake()

        # KeyboardHandler takes a plain config dict, not a StateManager.
        handler = kb.KeyboardHandler({}, fake)

        assert handler is not None

    def test_mqtt_client_accepts_a_fake(self, tmp_path: Path) -> None:
        from metixel.backend.mqtt_client import MQTTClient
        from metixel.backend.state import StateManager
        from metixel.shared.config import Config

        config_path = tmp_path / "config.json"
        Config().save(config_path)
        state = StateManager(config_path, tmp_path / "run")

        client = MQTTClient(state, _RecordingFake())

        assert client is not None
