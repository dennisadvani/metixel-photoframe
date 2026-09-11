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


class TestPersistedLogLevel:
    """The file-handler level must be applied at CONSTRUCTION time.

    Regression guard: the level used to be applied only after the handler was
    added, and only if ``config.json`` already existed.  On a fresh device it
    does not (``Config.load`` creates it later, inside the daemon), so handlers
    were built at DEBUG and a device configured ``log_level: NONE`` still wrote
    a full log on its first run.  The frontend — starting second, when the file
    did exist — honoured NONE, so the two processes disagreed.
    """

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("DEBUG", logging.DEBUG),
            ("INFO", logging.INFO),
            ("WARNING", logging.WARNING),
            ("ERROR", logging.ERROR),
            ("NONE", main_mod._LOG_LEVEL_NONE),
        ],
    )
    def test_reads_persisted_level(self, tmp_path, value, expected) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(f'{{"system": {{"log_level": "{value}"}}}}', encoding="utf-8")
        assert main_mod._read_persisted_log_level(cfg) == expected

    def test_missing_config_defaults_to_none(self, tmp_path) -> None:
        """A fresh device has no config yet — default is NONE, per the schema."""
        assert main_mod._read_persisted_log_level(tmp_path / "absent.json") == (
            main_mod._LOG_LEVEL_NONE
        )

    def test_corrupt_config_defaults_to_none(self, tmp_path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text("{ not json", encoding="utf-8")
        assert main_mod._read_persisted_log_level(cfg) == main_mod._LOG_LEVEL_NONE

    def test_level_is_case_insensitive(self, tmp_path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text('{"system": {"log_level": "info"}}', encoding="utf-8")
        assert main_mod._read_persisted_log_level(cfg) == logging.INFO

    def test_handler_level_applied_with_no_config_present(
        self, clean_root_logger, tmp_path, monkeypatch
    ) -> None:
        """THE regression: no config.json ⇒ handler must still get NONE.

        Previously the handler was created at DEBUG and the level applied
        afterwards only when the file existed, so a fresh install logged
        everything regardless of the configured level.
        """
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)

        # Deliberately do NOT create config.json.
        main_mod._setup_logging(
            tmp_path / "config.json",
            logging.INFO,
            file_logging=True,
            mode="backend",
        )

        handlers = _root_file_handlers()
        assert handlers, "expected a file handler to be attached"
        assert handlers[0].level == main_mod._LOG_LEVEL_NONE, (
            "handler must be created with the persisted/default level, "
            "not left at DEBUG until the config happens to exist"
        )

    def test_handler_level_applied_from_existing_config(
        self, clean_root_logger, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)
        cfg = tmp_path / "config.json"
        cfg.write_text('{"system": {"log_level": "WARNING"}}', encoding="utf-8")

        main_mod._setup_logging(cfg, logging.INFO, file_logging=True, mode="backend")

        handlers = _root_file_handlers()
        assert handlers, "expected a file handler to be attached"
        assert handlers[0].level == logging.WARNING

    def test_both_processes_agree_on_level(
        self, clean_root_logger, tmp_path, monkeypatch
    ) -> None:
        """Backend and frontend must resolve the SAME file level.

        The original symptom was an asymmetry: the backend logged everything
        while the frontend logged nothing, because only one of them saw the
        config file at startup.
        """
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)
        cfg = tmp_path / "config.json"
        cfg.write_text('{"system": {"log_level": "INFO"}}', encoding="utf-8")

        main_mod._setup_logging(cfg, logging.INFO, file_logging=True, mode="backend")
        backend_level = _root_file_handlers()[0].level

        logging.getLogger().handlers[:] = []
        main_mod._setup_logging(cfg, logging.INFO, file_logging=True, mode="frontend")
        frontend_level = _root_file_handlers()[0].level

        assert backend_level == frontend_level == logging.INFO


class TestPerProcessLogFiles:
    """Each process writes its OWN file — never a shared one."""

    def test_backend_and_frontend_use_distinct_paths(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)
        backend = main_mod._log_file_for_mode("backend")
        frontend = main_mod._log_file_for_mode("frontend")
        assert backend != frontend
        assert backend.name == "metixel-backend.log"
        assert frontend.name == "metixel-frontend.log"

    def test_unknown_mode_falls_back_to_generic_name(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)
        assert main_mod._log_file_for_mode(None).name == "metixel.log"


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

class TestPersistedLogLevelAtStartup:
    """The persisted ``system.log_level`` must apply even on a FRESH device.

    Regression guard: ``config.json`` does not exist on a fresh install — the
    daemon creates it AFTER logging is configured.  The level used to be applied
    to an already-created handler only when the config file happened to exist,
    so a device configured ``log_level: NONE`` still wrote a full log on its
    first run, while the frontend (starting second, once the file existed)
    honoured NONE.  The two processes disagreed.
    """

    def _with_config(self, tmp_path, level: str) -> "object":
        import json

        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"system": {"log_level": level}}), encoding="utf-8")
        return cfg

    def test_none_disables_file_logging(self, clean_root_logger, tmp_path, monkeypatch):
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)
        cfg = self._with_config(tmp_path, "NONE")

        main_mod._setup_logging(cfg, logging.INFO, file_logging=True, mode="backend")

        handlers = _root_file_handlers()
        assert handlers, "a file handler should still be attached"
        # NONE == above CRITICAL, so nothing reaches disk.
        assert all(h.level >= logging.CRITICAL for h in handlers)

    def test_info_level_is_applied(self, clean_root_logger, tmp_path, monkeypatch):
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)
        cfg = self._with_config(tmp_path, "INFO")

        main_mod._setup_logging(cfg, logging.INFO, file_logging=True, mode="backend")

        assert all(h.level == logging.INFO for h in _root_file_handlers())

    def test_debug_level_is_applied(self, clean_root_logger, tmp_path, monkeypatch):
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)
        cfg = self._with_config(tmp_path, "DEBUG")

        main_mod._setup_logging(cfg, logging.INFO, file_logging=True, mode="backend")

        assert all(h.level == logging.DEBUG for h in _root_file_handlers())

    def test_missing_config_defaults_to_none_not_debug(
        self, clean_root_logger, tmp_path, monkeypatch
    ) -> None:
        """THE fresh-install case: no config.json at all.

        Must NOT fall back to DEBUG (which is what produced the unbounded log),
        and must not raise.
        """
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)
        cfg = tmp_path / "config.json"
        assert not cfg.exists()

        main_mod._setup_logging(cfg, logging.INFO, file_logging=True, mode="backend")

        handlers = _root_file_handlers()
        assert handlers
        assert all(h.level >= logging.CRITICAL for h in handlers), (
            "a fresh device must default to NONE, not DEBUG"
        )

    def test_corrupt_config_does_not_raise(self, clean_root_logger, tmp_path, monkeypatch):
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)
        cfg = tmp_path / "config.json"
        cfg.write_text("{ not json", encoding="utf-8")

        # Must degrade to the safe default rather than crash the daemon.
        main_mod._setup_logging(cfg, logging.INFO, file_logging=True, mode="backend")
        assert all(h.level >= logging.CRITICAL for h in _root_file_handlers())

    def test_helper_reads_level_without_side_effects(self, tmp_path) -> None:
        """``_read_persisted_log_level`` must not create or modify config.json —
        creating it here would defeat the very race it exists to avoid."""
        cfg = tmp_path / "config.json"
        assert main_mod._read_persisted_log_level(cfg) == main_mod._LOG_LEVEL_NONE
        assert not cfg.exists(), "the helper must not create the config file"


class TestUnwritableLogFileGuard:
    """Graceful degradation when metixel.log cannot be opened for writing.

    A root-owned or otherwise unwritable metixel.log must never crash the
    daemon at startup (see the root-owned crash-loop bug referenced in the
    module docstring).  ``_setup_logging`` should fall back to console + ring
    buffer only and not attach any FileHandler.
    """

    def test_rotating_handler_open_failure_is_graceful(
        self, clean_root_logger, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)

        def _boom(*args, **kwargs):
            raise PermissionError(
                13, "Permission denied", str(tmp_path / "logs" / "metixel.log")
            )

        monkeypatch.setattr(logging.handlers, "RotatingFileHandler", _boom)

        # Must not raise, and must not attach any file handler.
        main_mod._setup_logging(
            tmp_path / "config.json", logging.DEBUG, file_logging=True
        )

        assert _root_file_handlers() == []

    def test_unwritable_log_dir_is_graceful(
        self, clean_root_logger, tmp_path, monkeypatch
    ) -> None:
        # Make data_dir()/logs a plain file so the mkdir() inside _setup_logging
        # raises FileExistsError (an OSError) instead of creating a directory.
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)
        (tmp_path / "logs").write_text("not a directory", encoding="utf-8")

        main_mod._setup_logging(
            tmp_path / "config.json", logging.DEBUG, file_logging=True
        )

        assert _root_file_handlers() == []

