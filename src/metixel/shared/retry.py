# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Retry / backoff helpers.

Centralises the hand-rolled retry loops (fixed-delay and exponential
backoff) that appeared in the immich downloader, the update manager's
poll/retry cycle, and elsewhere — and provides the reusable exponential-
backoff primitive the architecture doc mandates for network-dependent
sync work.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Sequence
from typing import TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def backoff_delays(
    attempts: int,
    *,
    base: float = 5.0,
    factor: float = 2.0,
    cap: float | None = None,
) -> Iterator[float]:
    """Yield the sleep delay before each retry (exponential backoff).

    For ``attempts`` total tries there are ``attempts - 1`` delays.
    ``base * factor ** i`` with an optional ``cap``.
    """
    for i in range(max(0, attempts - 1)):
        delay = base * (factor**i)
        if cap is not None:
            delay = min(delay, cap)
        yield delay


def retry(
    func: Callable[[], _T],
    *,
    attempts: int = 3,
    base: float = 5.0,
    factor: float = 2.0,
    cap: float | None = None,
    exceptions: Sequence[type[BaseException]] = (Exception,),
    on_retry: Callable[[BaseException, float, int], None] | None = None,
) -> _T:
    """Call *func* with exponential backoff, retrying on *exceptions*.

    Args:
        func: The callable to invoke (no-argument).
        attempts: Total number of attempts (>= 1).
        base: Initial delay in seconds before the first retry.
        factor: Multiplier applied to the delay after each failure.
        cap: Optional maximum delay.
        exceptions: Exception types that trigger a retry.
        on_retry: Optional callback ``(exc, delay, attempt)`` invoked
                  before sleeping (attempt is 1-based).

    Returns the first successful result.  If all attempts fail, the last
    exception is re-raised.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except exceptions as exc:  # type: ignore[misc]
            last_exc = exc
            if attempt >= attempts:
                break
            delay = (
                min(base * (factor ** (attempt - 1)), cap)
                if cap is not None
                else base * (factor ** (attempt - 1))
            )
            if on_retry is not None:
                on_retry(exc, delay, attempt)
            logger.warning(
                "Attempt %d/%d failed (%s); retrying in %.1fs",
                attempt,
                attempts,
                type(exc).__name__,
                delay,
            )
            time.sleep(delay)

    assert last_exc is not None
    raise last_exc
