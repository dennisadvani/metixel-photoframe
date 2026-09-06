# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Functional tests: MQTT / Home Assistant integration.

These run ON the Pi against the RUNNING backend.  They verify the MQTT
configuration round-trips through the API and that the broker-status endpoint
(*/system/mqtt-status*) reports a sane state for the configured broker.

The tests require MQTT credentials in ``functional/.env``:

    METIXEL_TEST_MQTT_BROKER=192.168.1.11
    METIXEL_TEST_MQTT_PORT=1883
    METIXEL_TEST_MQTT_USERNAME=
    METIXEL_TEST_MQTT_PASSWORD=

Only the broker + port are required; username/password are optional (many
local brokers run open).  If the broker is left empty the whole suite skips.

The ``/system/mqtt-status`` endpoint reflects the RUNNING backend's state.
Because the MQTT client connects only on backend start (it is started at boot
when ``config.mqtt.enabled`` is true), these tests deliberately do NOT force a
service restart.  The config-save test restores the original MQTT settings on
teardown so the frame is left unchanged.  Connectivity is verified by the test
connecting DIRECTLY to the broker itself with ``paho-mqtt`` (no service restart
or service state involved).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path

import pytest

from conftest import _load_env_file  # noqa: F401

# paho-mqtt is installed on the Pi (it is the real MQTT backend adapter).  We
# import it lazily only in the connectivity test so the suite can still
# collect/run on machines without it (e.g. a dev laptop pre-push).
try:
    import paho.mqtt.client as mqtt_client
    _PAHO_AVAILABLE = True
except ImportError:
    mqtt_client = None
    _PAHO_AVAILABLE = False

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.functional

BACKEND_PORT = 8080
BASE = f"http://127.0.0.1:{BACKEND_PORT}"

#: How long to allow the direct broker connect to settle (seconds).
_CONNECT_WAIT = 30


def _broker_connects(broker: str, port: int, username: str, password: str, timeout: int = _CONNECT_WAIT) -> str:
    """Attempt a direct TCP + MQTT connect to the broker as a client.

    Returns the CONNACK result: ``'connected'`` on success, ``'auth_error'``
    when the broker rejects credentials (CONNACK 4/5), or ``'not_responding'``
    when no CONNACK arrives within *timeout*.

    This exercises the broker + credentials exactly like the real backend
    would, but from the test process — no service restart required.
    """
    if not _PAHO_AVAILABLE:
        pytest.skip("paho-mqtt not installed")

    result = ["error"]

    def _on_connect(client, userdata, flags, rc, properties=None):  # noqa: ANN001
        # paho v2 passes rc via an enum; legacy passes an int (0 = success).
        code = int(rc.value if hasattr(rc, "value") else rc)
        if code == 0:
            result[0] = "connected"
        elif code in (4, 5):
            result[0] = "auth_error"
        else:
            result[0] = f"rejected(rc={code})"

    # paho v2 requires an explicit CallbackAPIVersion; old paho ignores it.
    kwargs: dict = {}
    if hasattr(mqtt_client, "CallbackAPIVersion"):
        kwargs["callback_api_version"] = mqtt_client.CallbackAPIVersion.VERSION2
    client = mqtt_client.Client(**kwargs, client_id="metixel-functional-test")
    client.on_connect = _on_connect  # type: ignore[attr-defined]
    if username:
        client.username_pw_set(username, password or "")  # type: ignore[attr-defined]

    client.connect(broker, port, keepalive=15)  # type: ignore[attr-defined]
    client.loop_start()  # type: ignore[attr-defined]

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and result[0] == "error":
        time.sleep(0.2)

    client.loop_stop()  # type: ignore[attr-defined]
    try:
        client.disconnect()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    return result[0]


def _api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as resp:
        return json.loads(resp.read().decode())


def _api_put(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _mqtt_env() -> dict[str, str]:
    """The MQTT test credentials from ``functional/.env``."""
    env = _load_env_file(Path(__file__).resolve().parent / ".env")
    return {
        "broker": env.get("METIXEL_TEST_MQTT_BROKER", "").strip(),
        "port": env.get("METIXEL_TEST_MQTT_PORT", "1883").strip(),
        "username": env.get("METIXEL_TEST_MQTT_USERNAME", "").strip(),
        "password": env.get("METIXEL_TEST_MQTT_PASSWORD", "").strip(),
    }


@pytest.fixture(scope="module")
def mqtt_creds() -> dict[str, str]:
    """The MQTT test credentials, skipping the suite if no broker is set."""
    creds = _mqtt_env()
    if not creds["broker"]:
        pytest.skip("METIXEL_TEST_MQTT_BROKER not set in functional/.env")
    return creds


@pytest.fixture(scope="module")
def mqtt_section(mqtt_creds: dict[str, str]) -> dict:
    """Save the original MQTT config, apply the test settings, restore after.

    Modeled on test_immich.py's ``immich_configured`` fixture: the running
    backend's ``config.mqtt`` is captured, the test broker/port/creds are
    written via ``PUT /api/config/mqtt``, and the original is restored on
    teardown so the frame is left in its prior state.
    """
    original = _api_get("/api/config/mqtt")

    # Determine the effective port (an integer, defaulting to 1883).
    try:
        port = int(mqtt_creds["port"] or 1883)
    except ValueError:
        port = 1883

    try:
        _api_put(
            "/api/config/mqtt",
            {
                "broker": mqtt_creds["broker"],
                "port": port,
                "username": mqtt_creds["username"],
                "password": mqtt_creds["password"],
            },
        )
        # Enabling requires a backend restart to actually start the client,
        # which we deliberately avoid — so yield the CONFIGURED (saved but not
        # necessarily connected) state.  MQTT `enabled` is left as-is.
        yield mqtt_creds
    finally:
        _api_put("/api/config/mqtt", original)


def test_mqtt_config_round_trips(mqtt_section: dict[str, str]) -> None:
    """The broker/port/username/password must persist through the config API.

    This is the determinisic core assertion: the settings are written then
    read back from the running config (no service restart required).
    """
    saved = _api_get("/api/config/mqtt")

    assert saved.get("broker") == mqtt_section["broker"], (
        f"broker not persisted: expected {mqtt_section['broker']!r}, got {saved.get('broker')!r}"
    )
    assert int(saved.get("port", 0)) == int(mqtt_section["port"] or 1883), (
        f"port not persisted: expected {mqtt_section['port']!r}, got {saved.get('port')!r}"
    )
    assert saved.get("username", "") == mqtt_section["username"], (
        f"username not persisted: expected {mqtt_section['username']!r}, got {saved.get('username')!r}"
    )
    assert saved.get("password", "") == mqtt_section["password"], (
        f"password not persisted"
    )


def test_mqtt_status_shape(mqtt_creds: dict[str, str]) -> None:
    """The MQTT status endpoint must report the configured broker + port.

    Independently of whether a live connection is up (the client connects at
    boot), the status object must carry the broker/port we configured and a
    valid status string.
    """
    status = _api_get("/api/system/mqtt-status")

    assert "status" in status, f"/api/system/mqtt-status missing 'status': {status}"
    assert "enabled" in status, f"/api/system/mqtt-status missing 'enabled': {status}"
    assert status.get("enabled") in (True, False), status

    # The broker/port reported should match the config currently applied.
    cfg = _api_get("/api/config/mqtt")
    if cfg.get("broker"):
        assert status.get("broker") == cfg["broker"], status
    if cfg.get("port"):
        assert int(status.get("port", -1)) == int(cfg["port"]), status


def test_mqtt_connectivity_direct(mqtt_section: dict[str, str]) -> None:
    """The broker must accept a direct MQTT connect using the test credentials.

    Connects to the broker from the test process (port + username/password),
    independently of the backend's own MQTT-client lifecycle — so this passes
    even when MQTT is disabled in the running config, without restarting the
    backend service.

    We assert the broker is REACHABLE and, when credentials are supplied, that
    it does NOT return an auth rejection (CONNACK 4/5).  An open broker answers
    CONNACK 0.
    """
    broker = mqtt_section["broker"]
    try:
        port = int(mqtt_section["port"] or 1883)
    except ValueError:
        port = 1883
    username = mqtt_section.get("username", "")
    password = mqtt_section.get("password", "")

    outcome = _broker_connects(broker, port, username, password)

    # auth_error means the broker is reachable but the credentials are wrong —
    # a useful signal, but it does NOT prove the config is usable.  Fail on it
    # so the developer knows the .env credentials need fixing.
    assert outcome != "auth_error", (
        f"Broker {broker}:{port} rejected credentials (username={username!r}) — "
        "fix METIXEL_TEST_MQTT_* in functional/.env"
    )
    assert outcome in ("connected",), (
        f"Broker {broker}:{port} did not answer CONNACK 0 within {_CONNECT_WAIT}s "
        f"(got {outcome!r})"
    )
    logger.info("MQTT direct connect to %s:%s → %s", broker, port, outcome)