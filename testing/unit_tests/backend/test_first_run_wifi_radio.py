# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""First-run WiFi radio enablement — the one-shot latch.

The backend enables the WiFi radio exactly ONCE per device, on its first boot,
so a device imaged with WiFi disabled (rfkill soft block, wlan0 unmanaged) still
comes up reachable instead of stranding the user with no network and no AP
fallback.

The latch is ``network.wifi_radio_first_run_done``.  What matters here:

  * it fires on a device whose radio is off, and the marker is then written;
  * it does NOT touch the radio once the marker is set — this is the property
    that stops the app overriding a user who deliberately turned WiFi off;
  * a FAILED enable does not set the marker, so the next boot retries rather
    than silently giving up forever;
  * a device with no WiFi hardware is skipped and the marker stays unset.

This replaced a ``scripts/reconcile.sh`` block that re-asserted the radio on
every OTA.  Guarded here so it cannot silently regress into a periodic write.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock


class FakeIPC:
    """IPCClient stand-in (avoids the Pi-only Unix socket)."""

    def __init__(self) -> None:
        self.sent: list = []

    def send(self, msg) -> None:
        self.sent.append(msg)

    def close(self) -> None:
        pass


def _make_daemon(tmp_path: Path, monkeypatch):
    """Build a real BackendDaemon with a fake IPC client."""
    import metixel.backend.daemon as daemon_mod
    from metixel.shared.config import Config

    config_path = tmp_path / "config.json"
    Config().save(config_path)
    monkeypatch.setattr(daemon_mod, "IPCClient", FakeIPC)
    monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
    return daemon_mod.BackendDaemon(config_path)


def _patch_network(monkeypatch, *, hardware=True, radio_enabled=False, set_ok=True):
    """Patch the network_manager symbols the daemon imports lazily."""
    import metixel.backend.network_manager as nm

    calls: list[bool] = []

    monkeypatch.setattr(nm, "is_wifi_hardware_present", lambda: hardware)
    monkeypatch.setattr(nm, "is_wifi_radio_enabled", lambda: radio_enabled)

    def fake_set(enabled: bool) -> bool:
        calls.append(enabled)
        return set_ok

    monkeypatch.setattr(nm, "set_wifi_radio", fake_set)
    return calls


class TestFirstRunRadioEnable:
    def test_enables_radio_and_writes_marker(self, tmp_path: Path, monkeypatch) -> None:
        daemon = _make_daemon(tmp_path, monkeypatch)
        calls = _patch_network(monkeypatch, hardware=True, radio_enabled=False)

        assert daemon._state.config.network["wifi_radio_first_run_done"] is False
        daemon._ensure_first_run_wifi_radio()

        assert calls == [True]
        assert daemon._state.config.network["wifi_radio_first_run_done"] is True

    def test_marker_is_persisted_to_disk(self, tmp_path: Path, monkeypatch) -> None:
        daemon = _make_daemon(tmp_path, monkeypatch)
        _patch_network(monkeypatch, hardware=True, radio_enabled=False)
        daemon._ensure_first_run_wifi_radio()

        # A new StateManager reading the same file must see the latch — the
        # whole point is that it survives a reboot.
        from metixel.backend.state import StateManager

        reloaded = StateManager(daemon._config_path, run_dir=tmp_path / "run")
        assert reloaded.config.network["wifi_radio_first_run_done"] is True

    def test_noop_when_marker_already_set(self, tmp_path: Path, monkeypatch) -> None:
        daemon = _make_daemon(tmp_path, monkeypatch)
        daemon._state.update_config("network", {"wifi_radio_first_run_done": True})
        calls = _patch_network(monkeypatch, hardware=True, radio_enabled=False)

        daemon._ensure_first_run_wifi_radio()

        # The critical property: a user who turned WiFi off is NOT overridden.
        assert calls == []

    def test_no_radio_change_when_already_enabled(self, tmp_path: Path, monkeypatch) -> None:
        daemon = _make_daemon(tmp_path, monkeypatch)
        calls = _patch_network(monkeypatch, hardware=True, radio_enabled=True)

        daemon._ensure_first_run_wifi_radio()

        # Radio already on → don't call nmcli, but DO record that the first-run
        # decision has been made.
        assert calls == []
        assert daemon._state.config.network["wifi_radio_first_run_done"] is True

    def test_failure_does_not_set_marker(self, tmp_path: Path, monkeypatch) -> None:
        daemon = _make_daemon(tmp_path, monkeypatch)
        calls = _patch_network(monkeypatch, hardware=True, radio_enabled=False, set_ok=False)

        daemon._ensure_first_run_wifi_radio()

        assert calls == [True]
        # No marker → retried on the next boot instead of being swallowed.
        assert daemon._state.config.network["wifi_radio_first_run_done"] is False

    def test_skips_device_without_wifi_hardware(self, tmp_path: Path, monkeypatch) -> None:
        daemon = _make_daemon(tmp_path, monkeypatch)
        calls = _patch_network(monkeypatch, hardware=False)

        daemon._ensure_first_run_wifi_radio()

        assert calls == []
        # Marker deliberately unset: a device that later gains a WiFi interface
        # (e.g. a USB dongle) is still covered.
        assert daemon._state.config.network["wifi_radio_first_run_done"] is False

    def test_never_raises(self, tmp_path: Path, monkeypatch) -> None:
        daemon = _make_daemon(tmp_path, monkeypatch)
        import metixel.backend.network_manager as nm

        monkeypatch.setattr(
            nm, "is_wifi_hardware_present", mock.Mock(side_effect=RuntimeError("boom"))
        )

        # Must degrade gracefully — a WiFi problem must never stop the daemon.
        daemon._ensure_first_run_wifi_radio()

    def test_writes_marker_exactly_once(self, tmp_path: Path, monkeypatch) -> None:
        daemon = _make_daemon(tmp_path, monkeypatch)
        _patch_network(monkeypatch, hardware=True, radio_enabled=False)

        with mock.patch.object(
            daemon._state, "update_config", wraps=daemon._state.update_config
        ) as spy:
            daemon._ensure_first_run_wifi_radio()
            assert spy.call_count == 1
            # Second boot: marker set → no write at all (SD-card wear).
            daemon._ensure_first_run_wifi_radio()
            assert spy.call_count == 1


class TestReconcileNoLongerTouchesRadio:
    """reconcile.sh must not re-assert the radio.  Convergent state and
    user-owned state are contradictory — the old block undid a user's
    `nmcli radio wifi off` on every OTA."""

    def test_script_has_no_radio_commands(self) -> None:
        script = Path(__file__).resolve().parents[3] / "scripts" / "reconcile.sh"
        text = script.read_text(encoding="utf-8")

        # Strip comment lines — the rationale block legitimately *mentions*
        # these commands, it just must never execute them.
        code_lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        code = "\n".join(code_lines)

        assert "rfkill unblock" not in code
        assert "nmcli radio wifi on" not in code
        assert "nmcli device set" not in code

    def test_regulatory_domain_still_reconciled(self) -> None:
        # The regdom is a preference, not a toggle — removing the radio block
        # must not have taken it with it.
        script = Path(__file__).resolve().parents[3] / "scripts" / "reconcile.sh"
        text = script.read_text(encoding="utf-8")

        assert "cfg80211" in text
        assert "wifi_country" in text
