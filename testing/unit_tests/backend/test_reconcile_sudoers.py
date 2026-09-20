# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""``reconcile.sh`` must provision passwordless sudo for the ``pi`` user.

The backend runs as ``pi`` under systemd with no TTY and performs every
privileged action through ``sudo -n`` (``metixel.shared.subprocess.run_sudo``).
Without a NOPASSWD entry those calls fail with "a password is required" and
the feature breaks silently — AP/DHCP, reboot/shutdown, timezone, DDC/CI and
the OTA dependency self-heal all depend on it.

This was never provisioned by any version: the scripts assumed it because
Raspberry Pi OS used to ship ``/etc/sudoers.d/010_pi-nopasswd``, and the Pi
Foundation removed it.  These tests pin the fix (and its safety rails) so it
cannot be dropped again.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "reconcile.sh"


@pytest.fixture(scope="module")
def reconcile_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def reconcile_code(reconcile_text: str) -> str:
    """The script with comment lines stripped.

    The rationale block legitimately *mentions* commands it must never run
    (same convention as ``test_first_run_wifi_radio.py``).
    """
    return "\n".join(
        line for line in reconcile_text.splitlines() if not line.lstrip().startswith("#")
    )


class TestSudoersRuleIsProvisioned:
    def test_writes_the_nopasswd_rule(self, reconcile_code: str) -> None:
        assert "pi ALL=(ALL) NOPASSWD: ALL" in reconcile_code

    def test_targets_the_standard_conventional_path(self, reconcile_code: str) -> None:
        """``010_pi-nopasswd`` is the name the Pi ecosystem already uses.

        Keeping the conventional path means an image that *does* still ship the
        old file converges to the same state as one that does not, and nothing
        else on the device needs to learn a Metixel-specific name.
        """
        assert "/etc/sudoers.d/010_pi-nopasswd" in reconcile_code

    def test_installs_with_mode_0440(self, reconcile_code: str) -> None:
        """sudo refuses to read a sudoers.d file that is group/other-writable.

        ``install -m 0440`` is what enforces that, so a refactor to a plain
        redirect would silently stop taking effect.
        """
        assert re.search(r"install\s+-m\s+0440", reconcile_code)

    def test_visudo_validates_before_installing(self, reconcile_code: str) -> None:
        """A malformed file in /etc/sudoers.d can lock out sudo entirely."""
        assert "visudo -c -f" in reconcile_code

    def test_rolls_back_if_the_directory_becomes_invalid(self, reconcile_code: str) -> None:
        """Re-validate in place and remove the file if sudo is now broken.

        ``/etc/sudoers`` may ``#includedir`` this directory, in which case a
        bad file here breaks sudo for every user — not just pi.
        """
        # The bare "--c" check (no -f) validates the whole sudoers tree.
        assert re.search(r"visudo -c(\s|$)", reconcile_code)
        assert re.search(r"rm -f \"\$\{SUDOERS_FILE\}\"", reconcile_code)

    def test_requires_the_pi_user_to_exist(self, reconcile_code: str) -> None:
        """Never write a rule for a user that is not on the device."""
        assert re.search(r"id -u pi", reconcile_code)


class TestSudoersBlockStaysSafe:
    def test_does_not_touch_the_user_owned_radio(self, reconcile_code: str) -> None:
        """Guard the rule-16 exception: adding sudo must not re-add the radio block."""
        assert "rfkill unblock" not in reconcile_code
        assert "nmcli radio wifi on" not in reconcile_code

    def test_is_not_written_inside_a_loop_or_timer(self, reconcile_text: str) -> None:
        """Rule 9: no writes from a polling loop.

        The sudoers file is written at most once (idempotent via the
        already-valid check), never per-tick.
        """
        # There is no loop construct in the script at all.
        assert "while true" not in reconcile_text
        assert "for _ in" not in reconcile_text


class TestScriptIsSyntacticallyValid:
    def test_bash_syntax_check_passes(self) -> None:
        """A syntax error here would abort every install and OTA."""
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_section_numbers_are_sequential(self, reconcile_text: str) -> None:
        """Sections must be uniquely and consecutively numbered.

        The boot-config section was renumbered after inserting sudoers; a
        duplicate or skipped number is a real (if cosmetic) regression because
        the rationale comments cross-reference sections by number.
        """
        numbers = [int(n) for n in re.findall(r"^# (\d+)\. ", reconcile_text, flags=re.MULTILINE)]
        assert numbers == list(range(1, len(numbers) + 1)), f"gaps or dups: {numbers}"
