# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the startup dependency self-heal (Option 2).

The self-heal ensures that after an OTA from an older release, newly-required
runtime dependencies (e.g. pillow-heif) are installed so a single upgrade
resolves them. It must never raise and must be a no-op when deps are present.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from metixel.backend.dependencies import (
    _DEFAULT_PIP,
    _parse_requirement_name,
    ensure_runtime_dependencies,
    missing_requirements,
)

# A name that will never be installed in the test environment.
_MISSING = "metixel-not-a-real-package-xyz"


def _fake_run(returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="ok", stderr="")


class TestParseRequirementName:
    def test_simple_name(self) -> None:
        assert _parse_requirement_name("pillow-heif>=0.16") == "pillow-heif"

    def test_name_with_extras_and_marker(self) -> None:
        line = 'SomePkg[foo]; python_version < "3.12"'
        assert _parse_requirement_name(line) == "SomePkg"

    def test_name_with_multiple_versions(self) -> None:
        assert _parse_requirement_name("numpy>=1.24,<2.0") == "numpy"

    def test_comment_and_blank(self) -> None:
        assert _parse_requirement_name("# comment") is None
        assert _parse_requirement_name("") is None
        assert _parse_requirement_name("   ") is None


class TestMissingRequirements:
    def test_missing_reported(self, tmp_path: Path) -> None:
        req = tmp_path / "requirements-pip.txt"
        req.write_text(f"# comment\n\n{_MISSING}>=1.0\n", encoding="utf-8")
        missing = missing_requirements(req)
        assert missing == [_MISSING]

    def test_missing_file_is_noop(self, tmp_path: Path) -> None:
        assert missing_requirements(tmp_path / "nope.txt") == []


class TestEnsureRuntimeDependencies:
    def test_installs_missing(self, tmp_path: Path) -> None:
        req = tmp_path / "requirements-pip.txt"
        req.write_text(f"{_MISSING}>=1.0\n", encoding="utf-8")
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _fake_run(0)

        installed = ensure_runtime_dependencies(
            tmp_path,
            run=fake_run,  # type: ignore[arg-type]
        )
        assert installed == [_MISSING]
        assert len(calls) == 1
        # The pip command targets requirements-pip.txt with --break-system-packages.
        assert calls[0][: len(_DEFAULT_PIP)] == list(_DEFAULT_PIP)
        assert calls[0][-2:] == ["-r", str(req)]

    def test_install_failure_is_logged_not_raised(self, tmp_path: Path) -> None:
        req = tmp_path / "requirements-pip.txt"
        req.write_text(f"{_MISSING}>=1.0\n", encoding="utf-8")

        def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return _fake_run(1)

        # Should return the missing package, not raise.
        installed = ensure_runtime_dependencies(
            tmp_path,
            run=fake_run,  # type: ignore[arg-type]
        )
        assert installed == [_MISSING]

    def test_noop_when_no_requirement_file(self, tmp_path: Path) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _fake_run(0)

        installed = ensure_runtime_dependencies(
            tmp_path,
            run=fake_run,  # type: ignore[arg-type]
        )
        assert installed == []
        assert calls == []
