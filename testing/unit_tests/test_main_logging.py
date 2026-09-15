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
from pathlib import Path

import pytest

import metixel.__main__ as main_mod
from metixel.shared import logging_setup
from metixel.shared.log_buffer import LogRingBuffer


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

    def test_both_processes_agree_on_level(self, clean_root_logger, tmp_path, monkeypatch) -> None:
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


class TestLogRotation:
    """Both per-process logs must be SIZE-ROTATED, never unbounded.

    Regression guard for a silent gap: the rotation parameters previously had
    no test at all.  Dropping ``maxBytes``/``backupCount`` from
    ``_setup_logging`` would have left every test green — the handler is still
    a ``RotatingFileHandler``, it simply never rotates — while the Pi's SD card
    slowly filled.  A log that grows without bound beside the running app is
    exactly the flash-wear failure mode core rule 9 forbids, so the bound is
    asserted per process rather than assumed.
    """

    @pytest.mark.parametrize("mode", ["backend", "frontend"])
    def test_both_logs_rotate_at_10_mib_keeping_5_backups(
        self, clean_root_logger, tmp_path, monkeypatch, mode
    ) -> None:
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)

        main_mod._setup_logging(
            tmp_path / "config.json", logging.INFO, file_logging=True, mode=mode
        )

        handlers = [
            h for h in _root_file_handlers() if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert handlers, f"no rotating file handler attached for mode={mode!r}"
        handler = handlers[0]

        # 10 MiB per file, 5 backups → 60 MiB per process, bounded.
        assert handler.maxBytes == 10_485_760
        assert handler.backupCount == 5

        # The handler must point at THIS process's own file: two processes
        # rotating one shared path truncate each other's output, which is the
        # bug the per-process split exists to prevent.
        expected = main_mod._log_file_for_mode(mode).name
        assert Path(handler.baseFilename).name == expected


class TestSetupLoggingFileHandler:
    def test_file_logging_creates_metixel_log(
        self, clean_root_logger, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)
        log_file = tmp_path / "logs" / "metixel.log"

        main_mod._setup_logging(tmp_path / "config.json", logging.DEBUG, file_logging=True)

        # A RotatingFileHandler is attached and the file actually exists.
        assert any(
            isinstance(h, logging.handlers.RotatingFileHandler) for h in clean_root_logger.handlers
        )
        assert log_file.is_file()

    def test_no_file_logging_never_creates_metixel_log(
        self, clean_root_logger, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)
        log_file = tmp_path / "logs" / "metixel.log"

        main_mod._setup_logging(tmp_path / "config.json", logging.DEBUG, file_logging=False)

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
            raise PermissionError(13, "Permission denied", str(tmp_path / "logs" / "metixel.log"))

        monkeypatch.setattr(logging.handlers, "RotatingFileHandler", _boom)

        # Must not raise, and must not attach any file handler.
        main_mod._setup_logging(tmp_path / "config.json", logging.DEBUG, file_logging=True)

        assert _root_file_handlers() == []

    def test_unwritable_log_dir_is_graceful(self, clean_root_logger, tmp_path, monkeypatch) -> None:
        # Make data_dir()/logs a plain file so the mkdir() inside _setup_logging
        # raises FileExistsError (an OSError) instead of creating a directory.
        monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)
        (tmp_path / "logs").write_text("not a directory", encoding="utf-8")

        main_mod._setup_logging(tmp_path / "config.json", logging.DEBUG, file_logging=True)

        assert _root_file_handlers() == []


@pytest.fixture
def metixel_logger_level():
    """Restore the package logger's level.

    Both :func:`~metixel.shared.logging_setup.apply_level` and ``_setup_logging``
    set it on a module-global logger, so a test that changes it must put it back
    or it leaks into every later test in the run.
    """
    metixel = logging.getLogger("metixel")
    before = metixel.level
    yield metixel
    metixel.setLevel(before)


def _configure(tmp_path, monkeypatch, level: str, *, terminal: int = logging.INFO) -> Path:
    """Run ``_setup_logging`` with *level* persisted; return the log file path."""
    import json

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"system": {"log_level": level}}), encoding="utf-8")
    monkeypatch.setattr(main_mod, "data_dir", lambda: tmp_path)
    main_mod._setup_logging(config, terminal, file_logging=True)
    return tmp_path / "logs" / "metixel.log"


def _flush() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


class TestLogLevelMap:
    """``system.log_level`` has exactly one name → level map."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("DEBUG", logging.DEBUG),
            ("info", logging.INFO),  # case-insensitive: config.json is hand-editable
            ("Warning", logging.WARNING),
            ("ERROR", logging.ERROR),
            ("NONE", logging_setup.NONE),
        ],
    )
    def test_parses_known_names(self, name, expected) -> None:
        assert logging_setup.parse_level(name) == expected

    @pytest.mark.parametrize("name", ["", "off", "TRACE", None, 42])
    def test_unknown_names_fall_back_to_none(self, name) -> None:
        """A bad value in ``config.json`` must not stop the frame booting."""
        assert logging_setup.parse_level(name) == logging_setup.NONE

    def test_names_round_trip(self) -> None:
        for name in logging_setup.LOG_LEVELS:
            assert logging_setup.level_name(logging_setup.parse_level(name)) == name

    def test_none_sits_above_critical(self) -> None:
        """The sentinel must filter out *everything*, not just CRITICAL."""
        assert logging_setup.NONE > logging.CRITICAL

    def test_live_view_is_never_quieter_than_info(self) -> None:
        """The Logs card must not be blank under the default ``NONE``."""
        assert logging_setup.live_view_level(logging_setup.NONE) == logging.INFO
        assert logging_setup.live_view_level(logging.ERROR) == logging.INFO
        assert logging_setup.live_view_level(logging.DEBUG) == logging.DEBUG


class TestLevelsActuallyTakeEffect:
    """Regression guards for a setting that silently did nothing.

    ``logging`` filters at two independent points: the logger decides whether a
    record is *created*, each handler decides whether it is *written*.  Setting
    only handler levels therefore cannot enable anything below the logger's level.

    ``system.log_level`` used to be wired to the handlers alone while the root
    logger stayed at INFO, so choosing "Debug" produced no debug output anywhere —
    not in the log file, and not in the dashboard's live view (whose ring buffer
    claimed to always capture DEBUG, so the Logs card's Debug filter could never
    show a single line).

    Every earlier test in this file asserted *handler* levels, which is why they
    all stayed green through the whole bug.  These assert the observable outcome
    instead: that a ``logger.debug`` call actually lands somewhere.
    """

    def test_debug_line_reaches_the_file(
        self, clean_root_logger, metixel_logger_level, tmp_path, monkeypatch
    ) -> None:
        log_file = _configure(tmp_path, monkeypatch, "DEBUG")

        logging.getLogger("metixel.regression").debug("DEBUG-MARKER")
        _flush()

        assert "DEBUG-MARKER" in log_file.read_text(encoding="utf-8"), (
            "selecting log_level=DEBUG must put debug lines in the log file"
        )

    def test_logger_is_opened_up_to_serve_the_file(
        self, clean_root_logger, metixel_logger_level, tmp_path, monkeypatch
    ) -> None:
        _configure(tmp_path, monkeypatch, "DEBUG")
        assert logging.getLogger("metixel").level == logging.DEBUG

    def test_none_silences_the_file_but_not_the_live_view(
        self, clean_root_logger, metixel_logger_level, tmp_path, monkeypatch
    ) -> None:
        """The two sinks are deliberately different levels.

        ``NONE`` is the default and exists to stop SD-card wear, not to make the
        dashboard's diagnostics useless — so the file goes quiet while the RAM-only
        live view keeps Info and above.
        """
        log_file = _configure(tmp_path, monkeypatch, "NONE")

        logging.getLogger("metixel.regression").info("INFO-MARKER")
        _flush()

        assert "INFO-MARKER" not in log_file.read_text(encoding="utf-8")
        buffer = logging_setup.ring_buffer()
        assert buffer is not None
        assert any(entry["message"] == "INFO-MARKER" for entry in buffer.get_recent(50))
        # The logger is at the live-view level, not at NONE — otherwise the live
        # view could never receive anything either.
        assert logging.getLogger("metixel").level == logging.INFO

    def test_third_party_debug_is_not_opened_up(
        self, clean_root_logger, metixel_logger_level, tmp_path, monkeypatch
    ) -> None:
        """Only the ``metixel`` tree is opened up, never the root logger.

        urllib3 logs every connection-pool event at DEBUG, and the dashboard polls
        the API constantly — opening up the root logger would fill the 500-entry
        ring buffer with that noise and evict the lines the Logs card exists to
        show.
        """
        _configure(tmp_path, monkeypatch, "DEBUG")
        assert logging.getLogger().level == logging.INFO
        assert not logging.getLogger("urllib3").isEnabledFor(logging.DEBUG)

    def test_runtime_change_applies_the_same_way(
        self, clean_root_logger, metixel_logger_level, tmp_path, monkeypatch
    ) -> None:
        """What ``POST /api/logs/level`` does must match the startup path."""
        _configure(tmp_path, monkeypatch, "NONE")
        assert not logging.getLogger("metixel").isEnabledFor(logging.DEBUG)

        logging_setup.apply_level(logging_setup.parse_level("DEBUG"))

        assert logging.getLogger("metixel").isEnabledFor(logging.DEBUG)


class TestRingBufferCapture:
    def test_each_record_is_captured_exactly_once(
        self, clean_root_logger, metixel_logger_level, tmp_path, monkeypatch
    ) -> None:
        """The buffer must not be attached twice.

        It used to be added to *both* the root logger and ``metixel``.  A record
        propagates up the hierarchy and ``logging`` does not de-duplicate a
        handler shared between an ancestor and a descendant, so every Metixel line
        appeared **twice** on the Logs card.
        """
        _configure(tmp_path, monkeypatch, "INFO")
        buffer = logging_setup.ring_buffer()
        assert buffer is not None
        buffer.clear()

        log = logging.getLogger("metixel.regression")
        log.debug("MARKER-DEBUG")
        log.info("MARKER-INFO")
        log.warning("MARKER-WARNING")

        messages = [entry["message"] for entry in buffer.get_recent(50)]
        for marker in ("MARKER-INFO", "MARKER-WARNING"):
            assert messages.count(marker) == 1, f"{marker} captured {messages.count(marker)} times"

    def test_buffer_is_attached_to_the_root_logger(
        self, clean_root_logger, metixel_logger_level, tmp_path, monkeypatch
    ) -> None:
        """One attachment point, on root, so it sees Metixel *and* third-party lines."""
        _configure(tmp_path, monkeypatch, "INFO")
        root_buffers = [h for h in logging.getLogger().handlers if isinstance(h, LogRingBuffer)]
        assert len(root_buffers) == 1
        package_buffers = [
            h for h in logging.getLogger("metixel").handlers if isinstance(h, LogRingBuffer)
        ]
        assert package_buffers == []
