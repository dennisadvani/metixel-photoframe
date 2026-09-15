# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Logging configuration — the single owner of log levels.

Why this is not just a ``setLevel`` call
---------------------------------------
``logging`` filters at two independent points, and conflating them is the bug this
module exists to prevent:

* the **logger** decides whether a ``LogRecord`` is created at all
  (``Logger.isEnabledFor``);
* each **handler** then decides whether that record is written.

Setting only handler levels therefore cannot enable anything below the logger's
level: ``logger.debug()`` never builds a record, so no handler can receive one.
``system.log_level`` used to be wired to the handlers alone while the root logger
was pinned at INFO, so choosing "Debug" produced no debug output anywhere — not in
the log file, and not in the dashboard's live view, whose ring buffer is
documented as always capturing DEBUG (so the Logs card's Debug filter could never
show anything either).

The three levels, stated once
-----------------------------
``system.log_level`` is the **file** verbosity — it is what the user picks, and it
is the one thing that costs SD-card writes.

The dashboard's live view is a RAM-only ring buffer, so it is not throttled for
wear.  It always keeps :data:`LIVE_VIEW_LEVEL` and above, because a Logs card that
is blank under the default ``NONE`` would be useless, and it follows the setting
down to DEBUG when the user asks for detail.

The ``metixel`` logger is then opened up to whichever sink is most permissive and
no further, so record-creation cost is only paid when something can observe it.
The root logger is deliberately *not* opened up: it stays at the terminal level so
third-party DEBUG chatter (urllib3 logging every connection-pool event) is never
created and cannot flood the log file or evict useful lines from the ring buffer.

One owner, so the processes cannot disagree
-------------------------------------------
The name→level map and the apply logic previously existed in four places (the CLI
bootstrap, the API route, the frontend renderer, and the Logs card's HTML).  Each
process resolving ``system.log_level`` through its own copy is how the backend and
frontend previously disagreed about the same setting — one wrote a full DEBUG log
while the other honoured ``NONE``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from metixel.shared.log_buffer import LogRingBuffer

#: Sentinel level above CRITICAL (50).  No record can pass it, so it disables a
#: sink without needing a special case.
NONE = 100

#: The single name → level map.  Keys are what ``system.log_level`` may contain.
LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "NONE": NONE,
}

#: What the dashboard's live view always keeps, whatever the file setting is.
LIVE_VIEW_LEVEL = logging.INFO

#: The package logger.  Only this tree is opened up; see the module docstring.
PACKAGE_LOGGER = "metixel"


def parse_level(name: object) -> int:
    """Return the level for *name*, or :data:`NONE` for anything unrecognised.

    Unknown values fall back to ``NONE`` rather than raising: the name comes from
    ``config.json``, and a bad value there must not stop the frame booting.  The
    lookup is case-insensitive so a hand-edited ``"info"`` still works.
    """
    return LOG_LEVELS.get(str(name).strip().upper(), NONE)


def level_name(value: int) -> str:
    """The configured name for *value* (the inverse of :func:`parse_level`)."""
    for name, level in LOG_LEVELS.items():
        if level == value:
            return name
    return "NONE"


def live_view_level(file_level: int) -> int:
    """The ring buffer's level for a given file level.

    Never quieter than :data:`LIVE_VIEW_LEVEL`, so the dashboard always has
    something to show; follows the setting down to DEBUG when the user asks for
    detail.
    """
    return min(file_level, LIVE_VIEW_LEVEL)


def logger_level(file_level: int, *, terminal_level: int = LIVE_VIEW_LEVEL) -> int:
    """The level the ``metixel`` logger must be set to.

    Records are created before handlers filter them, so the logger has to be at
    least as permissive as the most permissive sink.  A handler's level can only
    ever remove records — never add them — which is why setting only handler
    levels silently did nothing.
    """
    return min(file_level, live_view_level(file_level), terminal_level)


def _iter_handlers() -> Iterator[logging.Handler]:
    """Yield every handler attached to any logger in this process, once each.

    Walks the root logger *and* the manager's logger dict.  Iterating the dict
    alone misses handlers attached directly to the root, and an earlier version
    of this walk missed handlers on named loggers such as
    ``metixel.backend.state``, which then escaped the level applied at startup.

    De-duplicating by identity matters: a handler reached along two paths must be
    configured once, and (for the ring buffer) must not be registered twice.
    """
    seen: set[int] = set()
    loggers: list[logging.Logger] = [logging.getLogger()]
    loggers.extend(
        candidate
        for candidate in logging.Logger.manager.loggerDict.values()
        if isinstance(candidate, logging.Logger)
    )

    for logger_obj in loggers:
        for handler in logger_obj.handlers:
            if id(handler) not in seen:
                seen.add(id(handler))
                yield handler


def ring_buffer() -> LogRingBuffer | None:
    """Return this process's ring buffer, or ``None`` if it has none.

    The single lookup the web API uses, so the buffer's *location* is decided
    here too — it is attached to the root logger only, and searching every logger
    keeps this working for a test that attaches one itself.
    """
    for handler in _iter_handlers():
        if isinstance(handler, LogRingBuffer):
            return handler
    return None


def apply_level(file_level: int, *, terminal_level: int = LIVE_VIEW_LEVEL) -> int:
    """Apply *file_level* across this process: loggers, file handlers, live view.

    The one place levels are applied.  The CLI bootstrap, the runtime API route
    and the frontend's config hot-reload all call it, so the two processes cannot
    resolve the same ``system.log_level`` differently.

    Console handlers are deliberately left alone — the terminal is for whoever is
    watching it, not for the user's file setting — but *terminal_level* still
    participates in the logger level, or a ``--debug`` run would have its console
    level set to DEBUG while the logger refused to create DEBUG records.

    Returns the level the package logger was set to, which is the useful thing to
    assert on (the handler levels alone were what the old tests checked, which is
    why this bug shipped).
    """
    effective = logger_level(file_level, terminal_level=terminal_level)
    logging.getLogger(PACKAGE_LOGGER).setLevel(effective)

    live = live_view_level(file_level)
    for handler in _iter_handlers():
        if isinstance(handler, LogRingBuffer):
            handler.setLevel(live)
        elif isinstance(handler, logging.FileHandler):
            handler.setLevel(file_level)

    return effective
