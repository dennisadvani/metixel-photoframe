# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Subprocess helpers — unified ``subprocess.run`` and delayed-sudo.

Centralises the ``subprocess.run(capture_output, text, timeout)`` and the
``sudo`` + delayed-restart patterns that were previously hand-rolled (with
divergent timeout handling and error shapes) across ``network_manager``,
``dispmanx_backend``, ``state``, ``update_manager``, ``time.py``,
``system.py`` and ``media.py``.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Sequence

logger = logging.getLogger(__name__)


def run_cmd(
    cmd: Sequence[str],
    *,
    timeout: float | None = None,
    check: bool = False,
    env: dict[str, str] | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, returning a text ``CompletedProcess``.

    Captures stdout/stderr as text (never blocks on pipes).  When
    ``check`` is True a non-zero exit raises ``subprocess.CalledProcessError``
    (with ``output``/``stderr`` populated for diagnostics); otherwise a
    non-zero exit is returned normally and callers inspect ``returncode``.
    ``input`` is written to the child's stdin (e.g. for ``chpasswd`` /
    ``smbpasswd -s`` which read the new password from stdin).
    """
    return subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        env=env,
        input=input,
    )


def run_sudo(
    cmd: Sequence[str],
    *,
    timeout: float | None = None,
    check: bool = False,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``sudo -n <cmd>`` with the same contract as :func:`run_cmd`.

    Requires a NOPASSWD sudoers entry (the default for the Pi ``pi`` user).
    ``input`` is forwarded to :func:`run_cmd`.
    """
    return run_cmd(["sudo", "-n", *cmd], timeout=timeout, check=check, input=input)


def schedule_sudo(
    cmd: Sequence[str],
    *,
    ok_message: str,
    fail_message: str,
    thread_name: str,
    delay: float = 2.0,
    timeout: float = 15.0,
) -> None:
    """Run ``sudo -n <cmd>`` in a background thread after a short delay.

    The delay lets an HTTP response flush before the service restarts or
    the system reboots/shuts down.  Failures are logged (never raised) so
    the endpoint returns immediately and errors surface in the journal.

    Args:
        cmd: The command and its arguments (without ``sudo``).
        ok_message: Logged on success.
        fail_message: Logged (with detail) on failure.
        thread_name: Name for the background thread.
        delay: Seconds to sleep before running the command.
        timeout: Subprocess timeout in seconds.
    """

    def _run() -> None:
        time.sleep(delay)
        try:
            result = run_sudo(cmd, timeout=timeout)
            if result.returncode != 0:
                tail = (result.stderr or result.stdout or "").strip()[-300:]
                logger.error(
                    "%s failed (rc=%d): %s",
                    fail_message,
                    result.returncode,
                    tail,
                )
            else:
                logger.info("%s", ok_message)
        except subprocess.TimeoutExpired:
            logger.error("%s timed out after %.0fs", fail_message, timeout)
        except FileNotFoundError:
            logger.error("%s: command not found", fail_message)
        except Exception as exc:  # noqa: BLE001
            logger.error("%s failed: %s", fail_message, exc)

    thread = threading.Thread(target=_run, daemon=True, name=thread_name)
    thread.start()
