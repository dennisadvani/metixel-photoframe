# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for :mod:`metixel.display.screenshot`.

The capture shells out to ``grim``, so every test here fakes
``subprocess.run`` — nothing in this file needs a Pi, a compositor or a
display.  What is worth pinning down is the behaviour the dashboard depends on:

* a successful capture writes a PNG with a unique, chronologically sortable
  name, and reports its size;
* **every** failure mode is reported, never raised — a screenshot that cannot
  be taken must not take the frame down with it (``AGENTS.md`` rule 7);
* the subprocess is launched with the same explicit Wayland environment
  ``wlr-randr`` uses, which is what lets the *backend* reach cage's socket;
* clearing is bounded to files directly inside the directory.

The last class asserts facts about non-Python host files (the package list and
``reconcile.sh``).  It lives here rather than in ``test_host_config_guards.py``
so that everything the screenshot feature depends on is verifiable in one place.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from metixel.display.hardware import wayland_env
from metixel.display.screenshot import (
    DEFAULT_SCREENSHOT_DIR,
    capture,
    clear_screenshots,
    resolve_screenshot_dir,
)
from metixel.shared.paths import data_dir

REPO_ROOT = Path(__file__).resolve().parents[3]

#: A minimal valid-looking PNG body — only the length matters to these tests.
_PNG = b"\x89PNG\r\n\x1a\n" + b"payload" * 8


def _fake_grim(*, rc: int = 0, stderr: str = "", stdout: str = "", write: bool = True):
    """Build a ``subprocess.run`` stand-in for grim.

    Writes the file grim was asked for (``args[1]``) unless *write* is false,
    which is how the "grim exited 0 but produced nothing" case is exercised.
    """
    calls: list[dict] = []

    def run(args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        if write:
            Path(args[1]).write_bytes(_PNG)
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


@pytest.fixture
def grim(monkeypatch: pytest.MonkeyPatch):
    """Patch the subprocess call and return the fake."""

    def _install(**kwargs):
        fake = _fake_grim(**kwargs)
        monkeypatch.setattr("metixel.display.screenshot.subprocess.run", fake)
        return fake

    return _install


class TestResolveScreenshotDir:
    def test_default_is_inside_the_media_tree(self) -> None:
        """The default must stay under data/media so the Samba share covers it.

        ``[metixel-media]`` is the only share ``reconcile.sh`` defines, so a
        default anywhere else would mean screenshots are unreachable without
        SSH — which is the whole reason for choosing this location.
        """
        assert resolve_screenshot_dir(None) == data_dir() / "media" / "screenshots"
        assert resolve_screenshot_dir("") == data_dir() / "media" / "screenshots"
        assert resolve_screenshot_dir("   ") == data_dir() / "media" / "screenshots"
        assert DEFAULT_SCREENSHOT_DIR == "media/screenshots/"

    def test_relative_path_resolves_under_data_dir(self) -> None:
        assert resolve_screenshot_dir("shots/") == data_dir() / "shots"

    def test_absolute_path_is_used_as_is(self, tmp_path: Path) -> None:
        assert resolve_screenshot_dir(tmp_path) == tmp_path


class TestCapture:
    def test_successful_capture_reports_the_file(self, grim, tmp_path: Path) -> None:
        grim()
        result = capture(tmp_path, now=1_700_000_000)

        assert result.ok
        assert result.path is not None
        # Matched by pattern, not by value: the stamp is local time, so an
        # exact assertion would only pass in the author's timezone.
        assert re.fullmatch(r"screenshot-\d{8}-\d{6}\.png", result.filename)
        assert result.size_bytes == len(_PNG)
        assert result.error == ""

    def test_destination_is_created_when_missing(self, grim, tmp_path: Path) -> None:
        """A desktop run has no reconcile.sh to have created the directory."""
        grim()
        target = tmp_path / "nested" / "screenshots"
        assert not target.exists()

        result = capture(target, now=1_700_000_000)

        assert result.ok
        assert target.is_dir()

    def test_second_capture_in_the_same_second_does_not_overwrite(
        self, grim, tmp_path: Path
    ) -> None:
        """Two clicks inside one second must both survive."""
        grim()
        first = capture(tmp_path, now=1_700_000_000)
        second = capture(tmp_path, now=1_700_000_000)

        assert first.path != second.path
        assert first.path is not None and first.path.exists()
        assert second.path is not None and second.path.exists()
        assert second.filename.endswith("-2.png"), (
            "the disambiguating suffix is only added when the name is taken"
        )

    def test_grim_is_given_the_wayland_environment(self, grim, tmp_path: Path) -> None:
        """This is what lets the BACKEND capture, not just the frontend.

        The backend service is not started by the cage unit, so it inherits no
        WAYLAND_DISPLAY of its own; without an explicit one grim cannot reach
        the compositor socket and capture always fails.
        """
        fake = grim()
        capture(tmp_path, now=1_700_000_000)

        env = fake.calls[0]["kwargs"]["env"]
        assert env["WAYLAND_DISPLAY"] == wayland_env()["WAYLAND_DISPLAY"]
        assert env["XDG_RUNTIME_DIR"] == wayland_env()["XDG_RUNTIME_DIR"]

    def test_wayland_env_returns_a_copy(self) -> None:
        """A caller adding variables must not mutate the shared default."""
        env = wayland_env()
        env["EXTRA"] = "1"
        assert "EXTRA" not in wayland_env()

    # -- Failure modes: all reported, none raised ---------------------------

    def test_nonzero_exit_is_reported(self, grim, tmp_path: Path) -> None:
        grim(rc=1, stderr="compositor does not support wlr-screencopy")
        result = capture(tmp_path, now=1_700_000_000)

        assert not result.ok
        assert "wlr-screencopy" in result.error
        assert result.path is None

    def test_missing_binary_names_the_package(self, monkeypatch, tmp_path: Path) -> None:
        """A device that has not taken the update that added grim lands here."""

        def boom(*_args, **_kwargs):
            raise FileNotFoundError

        monkeypatch.setattr("metixel.display.screenshot.subprocess.run", boom)
        result = capture(tmp_path, now=1_700_000_000)

        assert not result.ok
        assert "grim is not installed" in result.error

    def test_timeout_is_reported(self, monkeypatch, tmp_path: Path) -> None:
        def boom(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="grim", timeout=15)

        monkeypatch.setattr("metixel.display.screenshot.subprocess.run", boom)
        result = capture(tmp_path, now=1_700_000_000)

        assert not result.ok
        assert "timed out" in result.error.lower()

    def test_oserror_is_reported(self, monkeypatch, tmp_path: Path) -> None:
        def boom(*_args, **_kwargs):
            raise OSError("cannot allocate memory")

        monkeypatch.setattr("metixel.display.screenshot.subprocess.run", boom)
        result = capture(tmp_path, now=1_700_000_000)

        assert not result.ok
        assert "cannot allocate memory" in result.error

    def test_empty_output_is_an_error_and_is_deleted(self, grim, tmp_path: Path) -> None:
        """grim can exit 0 having written nothing; an empty PNG is worse.

        A zero-byte file looks like a successful capture in a file browser, so
        it must be removed and reported rather than left behind.
        """
        grim(write=False)
        # grim "succeeded" but a zero-byte file is what it left behind.
        result = capture(tmp_path, now=1_700_000_000)

        assert not result.ok
        assert "empty" in result.error
        assert list(tmp_path.iterdir()) == []

    def test_unwritable_destination_is_reported(self, monkeypatch, tmp_path: Path) -> None:
        """A bad Screenshot Dir must produce a message, not a traceback."""
        blocked = tmp_path / "file-not-dir"
        blocked.write_text("x", encoding="utf-8")

        result = capture(blocked / "screenshots", now=1_700_000_000)

        assert not result.ok
        assert "Cannot create" in result.error


class TestClearScreenshots:
    def test_deletes_files_and_reports_the_total(self, tmp_path: Path) -> None:
        for name in ("a.png", "b.png", "c.png"):
            (tmp_path / name).write_bytes(b"x" * 100)

        deleted, freed = clear_screenshots(tmp_path)

        assert deleted == 3
        assert freed == 300
        assert list(tmp_path.iterdir()) == []

    def test_subdirectories_are_left_alone(self, tmp_path: Path) -> None:
        """Only the flat directory is the screenshot folder."""
        (tmp_path / "keep").mkdir()
        (tmp_path / "keep" / "inner.png").write_bytes(b"x")
        (tmp_path / "top.png").write_bytes(b"x")

        deleted, _freed = clear_screenshots(tmp_path)

        assert deleted == 1
        assert (tmp_path / "keep" / "inner.png").exists()

    def test_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        """Nothing to clear is the correct answer, not a failure."""
        assert clear_screenshots(tmp_path / "does-not-exist") == (0, 0)

    def test_empty_directory_reports_zero(self, tmp_path: Path) -> None:
        assert clear_screenshots(tmp_path) == (0, 0)


class TestHostPrerequisites:
    """The feature needs a package and a directory that no Python code creates."""

    def test_grim_is_in_the_system_requirements(self) -> None:
        """requirements-system.txt is the single source of truth for packages.

        ``ota_install.sh`` reads it on every install and upgrade, so a new
        release adding grim here reaches existing devices on their next OTA.
        Without the line, a capture on a device that updated fails with
        "grim is not installed on this device".
        """
        text = (REPO_ROOT / "requirements-system.txt").read_text(encoding="utf-8")
        packages = [
            ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")
        ]
        assert "grim" in packages, (
            "grim must be listed in requirements-system.txt or every capture on "
            "a device that has taken an update fails."
        )

    def test_reconcile_creates_the_default_screenshot_dir(self) -> None:
        """reconcile.sh is the single owner of the data tree (rule 16).

        The backend cannot create a root-owned directory, so the default
        screenshot folder has to be created (and chowned) there.  It must stay
        under media/ so the existing [metixel-media] Samba share exposes it.
        """
        text = (REPO_ROOT / "scripts" / "reconcile.sh").read_text(encoding="utf-8")
        assert "media/screenshots" in text, (
            "reconcile.sh must create data/media/screenshots. Without it the "
            "directory is absent on a fresh device and appears only once "
            "someone takes a screenshot."
        )

    def test_default_screenshot_dir_is_not_a_watch_path(self) -> None:
        """Screenshots must not enter the slideshow.

        The default lives inside media/, so it is only safe because it is not
        one of the default watch paths.  If someone adds it, the frame would
        start showing its own screenshots.
        """
        from metixel.shared.config import DEFAULT_CONFIG

        watch_paths = {entry["path"] for entry in DEFAULT_CONFIG["sync"]["local"]["watch_paths"]}
        assert DEFAULT_SCREENSHOT_DIR not in watch_paths
        assert "media/screenshots/" not in watch_paths
