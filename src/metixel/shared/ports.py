# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Clean Architecture *ports* — contracts for external dependencies.

This module defines the outbound interfaces (ports) that Metixel's core
business logic depends on.  Concrete *adapters* implementing these ports
live in :mod:`metixel.shared.adapters`; they are wired in at the
composition root via dependency injection.

Why :class:`typing.Protocol`?
    Structural (duck) typing — any object that implements the required
    members satisfies the port without inheriting from it.  The core never
    imports third-party libraries (``requests``, ``paho-mqtt``, ``libcec``,
    ...) directly; it only knows these interfaces, and tests can substitute
    lightweight fakes.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "HttpResponse",
    "HttpGateway",
    "MqttGateway",
    "CecController",
    "IrSocket",
    "DisplayDriver",
    "DdcController",
    "Ports",
]


# ── HTTP transport ──────────────────────────────────────────────────────────


@runtime_checkable
class HttpResponse(Protocol):
    """Minimal HTTP response contract (structurally satisfied by ``requests.Response``)."""

    @property
    def status_code(self) -> int: ...

    @property
    def ok(self) -> bool: ...

    @property
    def text(self) -> str: ...

    def raise_for_status(self) -> None: ...

    def json(self) -> Any: ...

    def iter_content(self, chunk_size: int = 1) -> Iterator[bytes]: ...

    def __enter__(self) -> HttpResponse: ...

    def __exit__(self, *args: Any) -> None: ...


@runtime_checkable
class HttpGateway(Protocol):
    """HTTP transport port — used by the Immich client and OTA updates."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        stream: bool = False,
        timeout: Any = None,
    ) -> HttpResponse: ...

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        timeout: Any = None,
    ) -> HttpResponse: ...


# ── MQTT broker ─────────────────────────────────────────────────────────────


@runtime_checkable
class MqttGateway(Protocol):
    """MQTT broker port — Home Assistant integration."""

    def connect(self, host: str, port: int, *, keepalive: int = 60) -> None: ...

    def disconnect(self) -> None: ...

    def loop_start(self) -> None: ...

    def loop_stop(self) -> None: ...

    def publish(self, topic: str, payload: str, *, retain: bool = False) -> None: ...

    def subscribe(self, topic: str) -> None: ...

    def set_credentials(self, username: str, password: str) -> None: ...

    def set_will(self, topic: str, payload: str, *, retain: bool = False) -> None: ...

    def set_handlers(self, on_connect: Any, on_message: Any) -> None: ...


# ── HDMI-CEC hardware ───────────────────────────────────────────────────────


@runtime_checkable
class CecController(Protocol):
    """HDMI-CEC adapter port — TV remote control via ``libcec``."""

    def set_log_callback(self, fn: Any) -> None: ...

    def set_keypress_callback(self, fn: Any) -> None: ...

    def initialize(self, device_name: str = "Metixel Frame") -> None: ...

    def detect_and_open(self) -> str | None: ...

    def close(self) -> None: ...


# ── IR hardware ─────────────────────────────────────────────────────────────


@runtime_checkable
class IrSocket(Protocol):
    """LIRC Unix-domain socket port — IR remote input."""

    def connect(self, path: str) -> None: ...

    def settimeout(self, timeout: float) -> None: ...

    def recv(self, bufsize: int) -> bytes: ...

    def close(self) -> None: ...


# ── Display hardware ────────────────────────────────────────────────────────


@runtime_checkable
class DisplayDriver(Protocol):
    """Rendering surface port — satisfied by :class:`metixel.display.backend.DisplayBackend`.

    This is the contract the presentation layer renders through; the concrete
    implementation (pi3d / PyOpenGL / pygame / tkinter) is selected by the
    display factory and injected at the composition root.
    """

    width: int
    height: int

    def create(self) -> Any: ...

    def destroy(self) -> None: ...

    def loop_running(self) -> bool: ...

    def swap_buffers(self) -> None: ...

    def draw_rect(self, *args: Any, **kwargs: Any) -> None: ...

    def draw_image(self, *args: Any, **kwargs: Any) -> None: ...

    def draw_crossfade(self, *args: Any, **kwargs: Any) -> None: ...

    def load_texture(self, path: Any, **kwargs: Any) -> Any: ...

    def unload_texture(self, texture: Any) -> None: ...

    def update_texture(self, texture: Any, data: Any) -> None: ...

    def draw_text(self, *args: Any, **kwargs: Any) -> None: ...

    def clear(self) -> None: ...

    def display_power(self, on: bool) -> None: ...


# ── DDC/CI monitor control ──────────────────────────────────────────────────


@runtime_checkable
class DdcController(Protocol):
    """DDC/CI monitor control port — typically backed by ``ddcutil``."""

    def available(self) -> bool:
        """Return True when the underlying tool / bus is usable."""
        ...

    def detect(self) -> list[Any]:
        """Return detected monitors (``DdcMonitor`` instances)."""
        ...

    def capabilities(self, display: int) -> Any:
        """Probe VCP features for *display* (``DdcCapabilities``)."""
        ...

    def get_vcp(self, display: int, code: int) -> Any:
        """Read one VCP feature (``DdcVcpValue``)."""
        ...

    def set_vcp(self, display: int, code: int, value: int) -> None:
        """Write one VCP feature."""
        ...

    def reset_factory(self, display: int) -> None:
        """Restore the monitor to factory defaults (VCP 0x04)."""
        ...


# ── Injected dependency bundle ──────────────────────────────────────────────


@dataclass(frozen=True)
class Ports:
    """Bundle of injectable external dependencies (dependency inversion).

    Each field may be left ``None``; the consuming component then resolves
    the real adapter at runtime (default behaviour).  Tests inject fakes.
    """

    http: HttpGateway | None = None
    mqtt: MqttGateway | None = None
    cec: CecController | None = None
    ir: IrSocket | None = None
    display: DisplayDriver | None = None
    ddc: DdcController | None = None
