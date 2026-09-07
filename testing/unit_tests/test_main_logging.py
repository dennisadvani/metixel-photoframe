# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Unit tests for the CLI logging bootstrap in ``metixel/__main__.py``.

Regression guard for the root-owned ``metixel.log`` bug: the on-disk file must
only be opened by the pi-run daemons (``backend``/``frontend``).  Root-run entry
points (``cursor-hider`` daemon, ``--clear-web-password`` one-shot) must never
create it, or the file ends up owned by root and the pi backend crash-loops
(``PermissionError``).  See ``metixel-backend.service``'s ``ExecStartPre`` note.
"""

import logging
import logging.handlers

import pytest

import metixel.__main__ as main_mod


@pytest.fixture
def clean_root_logger():
    """Snapshot the root logger's handlers so tests start and end clean."""
    root = logging.getLogger()
    before = root.handlers[:]
    before_level = root.level
    root.handlers[:] = []
    yield root
    root.handlers[:] = before
    root.setLevel(before_level)


def _root_file_handlers() -> list:
    root = logging.getLogger()
    return [h for h in root.handlers if isinstance(h, logging.FileHandler)]


class TestWantsFileLogging:
    @pytest.mark.parametrize(
        "mode,expected",
        [
            ("backend", True),
            ("frontend", True),
            ("cursor-hider", False),
            (None, False),  # --clear-web-password runs without --mode
        ],
    )
    def test_only_pi_daemons_write_the_log(self, mode, expected) -> None:
        assert main_mod._wants_file_logging(mode) is expected


class TestSetupLoggingFileHandler:
    def test_file_logging_creates_metixel_log(
        self, clean_root_logger, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)
        log_file = tmp_path / "logs" / "metixel.log"

        main_mod._setup_logging(
            tmp_path / "config.json", logging.DEBUG, file_logging=True
        )

        # A RotatingFileHandler is attached and the file actually exists.
        assert any(
            isinstance(h, logging.handlers.RotatingFileHandler)
            for h in clean_root_logger.handlers
        )
        assert log_file.is_file()

    def test_no_file_logging_never_creates_metixel_log(
        self, clean_root_logger, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)
        log_file = tmp_path / "logs" / "metixel.log"

        main_mod._setup_logging(
            tmp_path / "config.json", logging.DEBUG, file_logging=False
        )

        # The root-running entry points must not open the pi-owned log at all.
        assert _root_file_handlers() == []
        assert not log_file.exists()
