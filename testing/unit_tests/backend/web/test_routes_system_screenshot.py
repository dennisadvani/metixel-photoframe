# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Route tests for the screenshot endpoints on the System card.

Exercised through the real ``create_app()`` (see ``conftest.py``), with the
capture itself stubbed: these assert the *route* contract — the response shape
the SPA reads, that the configured directory is the one used, and that a failed
capture is a clean 500 rather than a raised exception.

The capture module has its own tests in ``testing/unit_tests/display/
test_screenshot.py``; the clear endpoint is exercised here against real files
because deleting is the route's only job and stubbing it would test nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from metixel.display.screenshot import CaptureResult

system_mod = pytest.importorskip("metixel.backend.web.routes.system")


@pytest.fixture
def shots_dir(tmp_path: Path, mock_state) -> Path:
    """Point ``system.screenshot_dir`` at a temp directory.

    Goes through ``update_config`` rather than mutating ``state.config``:
    ``StateManager.config`` returns a *copy*, so an in-place edit would be
    silently discarded and the route would fall back to the real default.
    """
    dest = tmp_path / "screenshots"
    mock_state.update_config("system", {"screenshot_dir": str(dest)})
    return dest


class TestTakeScreenshot:
    def test_success_reports_the_file(
        self, client, shots_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        def fake_capture(destination, **_kwargs):
            seen["destination"] = destination
            return CaptureResult(
                ok=True, path=Path(destination) / "screenshot-20260915-120000.png", size_bytes=4096
            )

        monkeypatch.setattr(system_mod, "capture", fake_capture)
        resp = client.post("/api/system/screenshot")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "ok"
        assert body["file"] == "screenshot-20260915-120000.png"
        assert body["size_bytes"] == 4096
        assert body["path"].endswith("screenshot-20260915-120000.png")

    def test_uses_the_configured_directory(
        self, client, shots_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Screenshot Dir setting must be what the capture writes into."""
        seen: dict[str, object] = {}

        def fake_capture(destination, **_kwargs):
            seen["destination"] = destination
            return CaptureResult(ok=True, path=Path(destination) / "x.png", size_bytes=1)

        monkeypatch.setattr(system_mod, "capture", fake_capture)
        client.post("/api/system/screenshot")

        assert seen["destination"] == shots_dir

    def test_failure_is_a_clean_500(
        self, client, shots_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing grim must not surface as an unhandled exception.

        The frame keeps running regardless (rule 7); the dashboard's toast keys
        off the non-2xx status, since the shared API layer discards the body.
        """

        def fake_capture(_destination, **_kwargs):
            return CaptureResult(ok=False, error="grim is not installed on this device")

        monkeypatch.setattr(system_mod, "capture", fake_capture)
        resp = client.post("/api/system/screenshot")

        assert resp.status_code == 500
        assert resp.get_json()["status"] == "error"
        assert "grim" in resp.get_json()["message"]

    def test_get_never_captures(
        self, client, shots_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A capture writes to the SD card, so a GET must not trigger one.

        A GET is reachable by a link, a prefetch or a crawler — a disk write
        with no user action behind it is exactly what rule 9 forbids.  The
        route is POST-only, but the SPA catch-all answers unmatched GETs with
        index.html, so asserting 405 would be wrong; the invariant that matters
        is that no capture happens.
        """
        called: list[bool] = []

        def fake_capture(*_args, **_kwargs):
            called.append(True)
            return CaptureResult(ok=True)

        monkeypatch.setattr(system_mod, "capture", fake_capture)

        client.get("/api/system/screenshot")

        assert called == []


class TestClearScreenshots:
    def test_deletes_real_files_and_reports_totals(
        self, client, shots_dir: Path, mock_state
    ) -> None:
        shots_dir.mkdir(parents=True)
        (shots_dir / "a.png").write_bytes(b"x" * 100)
        (shots_dir / "b.png").write_bytes(b"x" * 100)

        resp = client.post("/api/system/screenshot/clear")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "ok"
        assert body["deleted_files"] == 2
        assert body["freed_bytes"] == 200
        assert body["freed_mb"] == 0.0
        assert list(shots_dir.iterdir()) == []

    def test_nothing_to_clear_is_still_a_success(self, client, shots_dir: Path, mock_state) -> None:
        """An empty folder is not an error — the button should just say so."""
        shots_dir.mkdir(parents=True)

        resp = client.post("/api/system/screenshot/clear")

        assert resp.status_code == 200
        assert resp.get_json()["deleted_files"] == 0
        assert resp.get_json()["message"] == "No screenshots to clear"

    def test_missing_directory_is_not_an_error(self, client, shots_dir: Path, mock_state) -> None:
        """The folder may not exist yet on a device that has never captured."""
        assert not shots_dir.exists()

        resp = client.post("/api/system/screenshot/clear")

        assert resp.status_code == 200
        assert resp.get_json()["deleted_files"] == 0
