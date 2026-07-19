# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""In-memory ring buffer for capturing recent log entries.

Provides a ``logging.Handler`` that stores the last N log records in a
thread-safe deque. The web API reads from this buffer to display live logs
without needing to tail a file or parse journald.
"""

from __future__ import annotations

import logging
from collections import deque
from threading import Lock


class LogRingBuffer(logging.Handler):
    """A logging handler that stores records in a fixed-size ring buffer.

    Thread-safe — all reads and writes are protected by a lock.

    Usage::

        buffer = LogRingBuffer(capacity=500)
        logging.getLogger("metixel").addHandler(buffer)

        # Later, from the web route:
        entries = buffer.get_recent(100)
    """

    def __init__(self, capacity: int = 500, level: int = logging.NOTSET) -> None:
        super().__init__(level=level)
        self._capacity = max(capacity, 1)
        self._buffer: deque[dict] = deque(maxlen=self._capacity)
        self._lock = Lock()

    def emit(self, record: logging.LogRecord) -> None:
        """Store a log record in the ring buffer."""
        entry = {
            "timestamp": self.formatter.formatTime(record, self.formatter.datefmt)
            if self.formatter
            else record.asctime or "",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        with self._lock:
            self._buffer.append(entry)

    def get_recent(self, count: int = 100) -> list[dict]:
        """Return the most recent *count* log entries (newest last)."""
        with self._lock:
            items = list(self._buffer)
            if count > 0:
                items = items[-count:]
            return items

    def clear(self) -> None:
        """Clear all buffered log entries."""
        with self._lock:
            self._buffer.clear()

    @property
    def count(self) -> int:
        """Current number of buffered entries."""
        with self._lock:
            return len(self._buffer)
