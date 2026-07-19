# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Sync scheduling — cron-like timing for sync operations."""

import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)


class SyncScheduler:
    """Simple interval-based scheduler for sync operations.

    Not a full cron — just runs a callback at a configurable interval
    with jitter to avoid thundering-herd problems.
    """

    def __init__(self, callback: Callable[[], None], interval_seconds: int, jitter: float = 0.1) -> None:
        self._callback = callback
        self._interval = interval_seconds
        self._jitter = jitter  # ±10% random jitter by default
        self._running = False

    def run(self) -> None:
        """Run the scheduler loop — blocks until stop() is called."""
        import random

        self._running = True
        while self._running:
            start = time.monotonic()
            try:
                self._callback()
            except Exception:
                logger.exception("Scheduled callback failed")
            elapsed = time.monotonic() - start
            # Add jitter to avoid lockstep with other pollers
            jitter = random.uniform(-self._jitter, self._jitter) * self._interval
            sleep_time = max(0, self._interval - elapsed + jitter)
            time.sleep(sleep_time)

    def stop(self) -> None:
        self._running = False
