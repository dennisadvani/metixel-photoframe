# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Clean Architecture ports — adapter conformance and composition-root wiring.

These tests exercise the dependency-inversion seam introduced in Phase 2/3:
the port ``Protocol``s in ``metixel.shared.ports``, their concrete adapters in
``metixel.shared.adapters``, and the composition-root factories
(``build_backend`` / ``build_renderer``).
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest


class TestAdapterConformance:
    """Real adapters must structurally satisfy their port Protocols."""

    def test_requests_http_gateway(self) -> None:
        from metixel.shared.adapters import RequestsHttpGateway
        from metixel.shared.ports import HttpGateway

        assert isinstance(RequestsHttpGateway(), HttpGateway)

    def test_paho_mqtt_gateway(self) -> None:
        pytest.importorskip("paho.mqtt.client")
        from metixel.shared.adapters import PahoMqttGateway
        from metixel.shared.ports import MqttGateway

        assert isinstance(PahoMqttGateway(), MqttGateway)

    def test_lib_cec_adapter(self) -> None:
        pytest.importorskip("cec")
        from metixel.shared.adapters import LibCecAdapter
        from metixel.shared.ports import CecController

        assert isinstance(LibCecAdapter(), CecController)

    def test_lirc_socket_adapter(self) -> None:
        if not hasattr(socket, "AF_UNIX"):
            pytest.skip("AF_UNIX not available on this platform")
        from metixel.shared.adapters import LircSocketAdapter
        from metixel.shared.ports import IrSocket

        assert isinstance(LircSocketAdapter(), IrSocket)

    def test_structural_fake_satisfies_http_port(self) -> None:
        """A duck-typed fake satisfies the port without subclassing."""
        from metixel.shared.ports import HttpGateway

        class FakeHttp:
            def get(self, *args, **kwargs):
                return None

            def post(self, *args, **kwargs):
                return None

        assert isinstance(FakeHttp(), HttpGateway)


class TestPortsBundle:
    def test_all_fields_default_to_none(self) -> None:
        from metixel.shared.ports import Ports

        ports = Ports()
        assert ports.http is None
        assert ports.mqtt is None
        assert ports.cec is None
        assert ports.ir is None
        assert ports.display is None

    def test_fields_are_injectable(self) -> None:
        from metixel.shared.adapters import RequestsHttpGateway
        from metixel.shared.ports import Ports

        http = RequestsHttpGateway()
        ports = Ports(http=http)
        assert ports.http is http


class _FakeIPC:
    """Minimal IPCClient stand-in (the real one uses a Pi-only Unix socket)."""

    def __init__(self) -> None:
        self.sent = []

    def send(self, *args, **kwargs) -> None:
        self.sent.append(args[0] if args else None)

    def close(self) -> None:
        pass


class _FakeIPCServer:
    def __init__(self) -> None:
        pass

    def stop(self) -> None:
        pass


class TestCompositionRoots:
    @staticmethod
    def _write_config(tmp_path: Path) -> Path:
        from metixel.shared.config import Config

        config_path = tmp_path / "config.json"
        Config().save(config_path)
        return config_path

    def test_build_backend_wires_injected_ports(self, tmp_path, monkeypatch) -> None:
        import metixel.backend.daemon as daemon_mod
        from metixel.shared.adapters import RequestsHttpGateway
        from metixel.shared.ports import Ports

        monkeypatch.setattr(daemon_mod, "IPCClient", _FakeIPC)
        http = RequestsHttpGateway()
        daemon = daemon_mod.build_backend(self._write_config(tmp_path), ports=Ports(http=http))
        assert daemon._ports.http is http

    def test_build_backend_default_ports(self, tmp_path, monkeypatch) -> None:
        import metixel.backend.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "IPCClient", _FakeIPC)
        daemon = daemon_mod.build_backend(self._write_config(tmp_path))
        assert daemon._ports.http is None
        assert daemon._ports.mqtt is None

    def test_build_renderer_injects_backend(self, tmp_path, monkeypatch) -> None:
        import metixel.frontend.renderer as renderer_mod

        monkeypatch.setattr(renderer_mod, "IPCServer", _FakeIPCServer)
        fake_backend = object()
        renderer = renderer_mod.build_renderer(self._write_config(tmp_path), backend=fake_backend)
        assert renderer._backend is fake_backend
