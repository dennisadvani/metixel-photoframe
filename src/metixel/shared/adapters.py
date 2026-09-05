# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Concrete *adapters* implementing the ports in :mod:`metixel.shared.ports`.

These are infrastructure adapters that wrap third-party libraries
(``requests``, ``paho-mqtt``, ``libcec``, Unix sockets) behind the port
interfaces.  The third-party imports are deferred so a missing library
degrades gracefully at runtime, and the adapters are wired in at the
composition root via dependency injection — the core never imports them
directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from metixel.shared.ddcutil_adapter import DdcutilAdapter
from metixel.shared.ports import (
    CecController,
    HttpGateway,
    HttpResponse,
    IrSocket,
    MqttGateway,
)

__all__ = [
    "RequestsHttpGateway",
    "PahoMqttGateway",
    "LibCecAdapter",
    "LircSocketAdapter",
    "DdcutilAdapter",
]


class RequestsHttpGateway(HttpGateway):
    """Adapts the :mod:`requests` library to :class:`HttpGateway`."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        stream: bool = False,
        timeout: Any = None,
    ) -> HttpResponse:
        import requests  # type: ignore[import-not-found, import-untyped]

        return requests.get(  # type: ignore[no-any-return]
            url, headers=headers, params=params, stream=stream, timeout=timeout
        )

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        timeout: Any = None,
    ) -> HttpResponse:
        import requests  # type: ignore[import-not-found, import-untyped]

        return requests.post(  # type: ignore[no-any-return]
            url, headers=headers, json=json, timeout=timeout
        )


class PahoMqttGateway(MqttGateway):
    """Adapts the :mod:`paho.mqtt` client to :class:`MqttGateway`."""

    def __init__(self) -> None:
        import paho.mqtt.client as mqtt  # type: ignore[import-not-found, import-untyped]

        # Use the current VERSION2 callback API so paho-mqtt 2.x doesn't
        # emit a DeprecationWarning (VERSION1 is deprecated in 2.x). Falls
        # back gracefully on paho-mqtt 1.x, where CallbackAPIVersion does
        # not exist and VERSION1 is the only (non-deprecated) option.
        kwargs: dict[str, Any] = {"client_id": f"metixel-{id(self)}"}
        if hasattr(mqtt, "CallbackAPIVersion"):
            kwargs["callback_api_version"] = mqtt.CallbackAPIVersion.VERSION2
        self._client = mqtt.Client(**kwargs)

    def connect(self, host: str, port: int, *, keepalive: int = 60) -> None:
        self._client.connect(host, port, keepalive=keepalive)

    def disconnect(self) -> None:
        self._client.disconnect()

    def loop_start(self) -> None:
        self._client.loop_start()

    def loop_stop(self) -> None:
        self._client.loop_stop()

    def publish(self, topic: str, payload: str, *, retain: bool = False) -> None:
        self._client.publish(topic, payload, retain=retain)

    def subscribe(self, topic: str) -> None:
        self._client.subscribe(topic)

    def set_credentials(self, username: str, password: str) -> None:
        self._client.username_pw_set(username, password)

    def set_will(self, topic: str, payload: str, *, retain: bool = False) -> None:
        self._client.will_set(topic, payload, retain=retain)

    def set_handlers(self, on_connect: Any, on_message: Any) -> None:
        self._client.on_connect = on_connect
        self._client.on_message = on_message


class LibCecAdapter(CecController):
    """Adapts the :mod:`cec` (libcec) module to :class:`CecController`."""

    def __init__(self) -> None:
        import cec  # type: ignore[import-not-found, import-untyped]

        self._cec = cec
        self._config: Any = None
        self._client: Any = None
        self._log_callback: Any = None
        self._key_callback: Any = None

    def set_log_callback(self, fn: Any) -> None:
        self._log_callback = fn

    def set_keypress_callback(self, fn: Any) -> None:
        self._key_callback = fn

    def initialize(self, device_name: str = "Metixel Frame") -> None:
        cec = self._cec
        config = cec.libcec_configuration()
        config.strDeviceName = device_name
        config.bActivateSource = 0
        config.deviceTypes.Add(cec.CEC_DEVICE_TYPE_PLAYBACK_DEVICE)
        config.clientVersion = cec.LIBCEC_VERSION_CURRENT
        config.SetLogCallback(self._log_callback)
        config.SetKeyPressCallback(self._key_callback)
        self._config = config
        self._client = cec.ICECAdapter.Create(config)

    def detect_and_open(self) -> str | None:
        if self._client is None:
            return None
        adapters = self._client.DetectAdapters()
        if not adapters:
            return None
        self._client.Open(adapters[0].strComName)
        return adapters[0].strComName  # type: ignore[no-any-return]

    def close(self) -> None:
        if self._client is not None:
            self._client.Close()


class LircSocketAdapter(IrSocket):
    """Adapts a Unix ``AF_UNIX`` stream socket to :class:`IrSocket` (LIRC)."""

    def __init__(self) -> None:
        import socket

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]

    def connect(self, path: str) -> None:
        self._sock.connect(path)

    def settimeout(self, timeout: float) -> None:
        self._sock.settimeout(timeout)

    def recv(self, bufsize: int) -> bytes:
        return self._sock.recv(bufsize)

    def close(self) -> None:
        self._sock.close()
