# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""``ddcutil`` CLI adapter for the :class:`~metixel.shared.ports.DdcController` port.

All I²C / DDC I/O goes through the system ``ddcutil`` binary so Metixel
never links against libddcutil.  Parsing is kept tolerant of minor stdout
format differences across ddcutil versions.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from typing import cast

from metixel.shared.ddc_types import (
    DdcCapabilities,
    DdcDiscreteOption,
    DdcFeature,
    DdcMonitor,
    DdcVcpValue,
    enrich_feature,
)
from metixel.shared.ports import DdcController

logger = logging.getLogger(__name__)

__all__ = [
    "DdcutilAdapter",
    "parse_detect",
    "parse_capabilities",
    "parse_getvcp",
]

# ddcutil probes the I²C bus with layered sleep/retry logic.  On marginal
# buses (cheap DDC-over-HDMI, e.g. a Pi 3 driving an older monitor) a single
# ``capabilities`` call routinely takes 5–9 s, so the old 5 s timeout made the
# probe time out intermittently and the UI report "no DDC-capable monitor".
# 15 s gives ample headroom while still failing eventually on a dead bus.
_DEFAULT_TIMEOUT = 15.0

_DISPLAY_RE = re.compile(r"^Display\s+(\d+)\b", re.IGNORECASE)
_MODEL_RE = re.compile(r"^\s*Model:\s*(.+)$", re.IGNORECASE)
_MFG_RE = re.compile(r"^\s*Mfg\s*id:\s*(.+)$", re.IGNORECASE)
_SERIAL_RE = re.compile(r"^\s*Serial\s*number:\s*(.+)$", re.IGNORECASE)
_I2C_RE = re.compile(r"^\s*I2C\s*bus:\s*(.+)$", re.IGNORECASE)
_VCP_VER_RE = re.compile(r"^\s*VCP\s*version:\s*(.+)$", re.IGNORECASE)
_MCCS_RE = re.compile(r"^\s*MCCS\s*version:\s*(.+)$", re.IGNORECASE)
_FEATURE_RE = re.compile(
    r"^\s*Feature:\s*([0-9A-Fa-f]{2})\s*(?:\(([^)]*)\))?",
    re.IGNORECASE,
)
_CONTINUOUS_RE = re.compile(r"continuous", re.IGNORECASE)
_TABLE_RE = re.compile(r"\btable\b", re.IGNORECASE)
_DISCRETE_VALUE_RE = re.compile(
    r"^\s*([0-9A-Fa-f]{2})\s*:\s*(.+)$",
)
_VALUES_HEX_LIST_RE = re.compile(
    r"Values:\s*((?:[0-9A-Fa-f]{2}\s*)+)",
    re.IGNORECASE,
)
_GETVCP_CONT_RE = re.compile(
    r"VCP\s+code\s+0x([0-9A-Fa-f]+)\s*(?:\(([^)]*)\))?\s*:"
    r".*?current\s+value\s*=\s*(\d+)\s*,\s*max\s+value\s*=\s*(\d+)",
    re.IGNORECASE | re.DOTALL,
)
_GETVCP_NC_RE = re.compile(
    r"VCP\s+code\s+0x([0-9A-Fa-f]+)\s*(?:\(([^)]*)\))?\s*:"
    r".*?current\s+value\s*=\s*0x([0-9A-Fa-f]+)",
    re.IGNORECASE | re.DOTALL,
)
# Discrete features may report the current value as a "simple level":
#   VCP code 0x14 (Select color preset): 6500 K (sl=0x05)
_GETVCP_SL_RE = re.compile(
    r"VCP\s+code\s+0x([0-9A-Fa-f]+)\s*(?:\(([^)]*)\))?\s*:"
    r".*?\(sl=0x([0-9A-Fa-f]+)\)",
    re.IGNORECASE | re.DOTALL,
)


def parse_detect(stdout: str) -> list[DdcMonitor]:
    """Parse ``ddcutil detect`` stdout into monitor records."""
    monitors: list[DdcMonitor] = []
    current: dict[str, object] | None = None

    def _flush() -> None:
        nonlocal current
        if current and "display" in current:
            monitors.append(
                DdcMonitor(
                    display=int(cast(int, current["display"])),
                    model=str(current.get("model", "")),
                    mfg=str(current.get("mfg", "")),
                    serial=str(current.get("serial", "")),
                    i2c_bus=str(current.get("i2c_bus", "")),
                    vcp_version=str(current.get("vcp_version", "")),
                )
            )
        current = None

    for line in stdout.splitlines():
        m = _DISPLAY_RE.match(line)
        if m:
            _flush()
            current = {"display": int(m.group(1))}
            continue
        if current is None:
            continue
        if m := _MODEL_RE.match(line):
            current["model"] = m.group(1).strip()
        elif m := _MFG_RE.match(line):
            current["mfg"] = m.group(1).strip()
        elif m := _SERIAL_RE.match(line):
            current["serial"] = m.group(1).strip()
        elif m := _I2C_RE.match(line):
            current["i2c_bus"] = m.group(1).strip()
        elif m := _VCP_VER_RE.match(line):
            current["vcp_version"] = m.group(1).strip()
    _flush()
    return monitors


def parse_capabilities(stdout: str, display: int) -> DdcCapabilities:
    """Parse ``ddcutil capabilities`` stdout into a feature list."""
    model = ""
    mccs = ""
    features: list[DdcFeature] = []
    current: DdcFeature | None = None
    in_vcp = False

    def _finish_feature() -> None:
        nonlocal current
        if current is not None:
            features.append(enrich_feature(current))
            current = None

    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        if m := _MODEL_RE.match(line):
            model = m.group(1).strip()
            continue
        if m := _MCCS_RE.match(line):
            mccs = m.group(1).strip()
            continue
        if re.match(r"^\s*VCP\s+Features\s*:", line, re.IGNORECASE):
            in_vcp = True
            continue
        if not in_vcp and line.lstrip().lower().startswith("feature:"):
            in_vcp = True

        if not in_vcp:
            continue

        m = _FEATURE_RE.match(line)
        if m:
            _finish_feature()
            code = int(m.group(1), 16)
            name = (m.group(2) or "").strip()
            current = DdcFeature(code=code, name=name)
            continue

        if current is None:
            continue

        lower = line.lower()
        if "values:" in lower:
            if _CONTINUOUS_RE.search(line):
                current.feature_type = "continuous"
            elif _TABLE_RE.search(line):
                current.feature_type = "table"
            else:
                hex_list = _VALUES_HEX_LIST_RE.search(line)
                if hex_list:
                    current.feature_type = "discrete"
                    for tok in hex_list.group(1).split():
                        try:
                            val = int(tok, 16)
                        except ValueError:
                            continue
                        if not any(o.value == val for o in current.options):
                            current.options.append(DdcDiscreteOption(value=val))
            continue

        dm = _DISCRETE_VALUE_RE.match(line)
        if dm:
            current.feature_type = "discrete"
            val = int(dm.group(1), 16)
            label = dm.group(2).strip()
            # Strip trailing "(means: ...)" noise if present
            label = re.sub(r"\s*\(means:.*$", "", label, flags=re.IGNORECASE).strip()
            existing = next((o for o in current.options if o.value == val), None)
            if existing:
                if label and not existing.label:
                    existing.label = label
            else:
                current.options.append(DdcDiscreteOption(value=val, label=label))

    _finish_feature()

    # Default unknown types: discrete if options present, else continuous.
    for feat in features:
        if feat.feature_type == "unknown":
            feat.feature_type = "discrete" if feat.options else "continuous"

    return DdcCapabilities(
        display=display,
        model=model,
        mccs_version=mccs,
        features=features,
        raw=stdout,
    )


def parse_getvcp(stdout: str, code: int | None = None) -> DdcVcpValue | None:
    """Parse a single ``ddcutil getvcp`` result."""
    text = stdout.strip()
    if not text:
        return None

    m = _GETVCP_CONT_RE.search(text)
    if m:
        parsed_code = int(m.group(1), 16)
        if code is not None and parsed_code != code:
            return None
        return DdcVcpValue(
            code=parsed_code,
            name=(m.group(2) or "").strip(),
            current=int(m.group(3)),
            maximum=int(m.group(4)),
            feature_type="continuous",
        )

    m = _GETVCP_NC_RE.search(text)
    if m:
        parsed_code = int(m.group(1), 16)
        if code is not None and parsed_code != code:
            return None
        return DdcVcpValue(
            code=parsed_code,
            name=(m.group(2) or "").strip(),
            current=int(m.group(3), 16),
            maximum=0,
            feature_type="discrete",
        )

    # "Simple level" form used by some discrete features (e.g. colour preset):
    #   VCP code 0x14 (Select color preset): 6500 K (sl=0x05)
    m = _GETVCP_SL_RE.search(text)
    if m:
        parsed_code = int(m.group(1), 16)
        if code is not None and parsed_code != code:
            return None
        return DdcVcpValue(
            code=parsed_code,
            name=(m.group(2) or "").strip(),
            current=int(m.group(3), 16),
            maximum=0,
            feature_type="discrete",
        )
    return None


class DdcutilAdapter(DdcController):
    """Adapts the ``ddcutil`` CLI to :class:`DdcController`."""

    def __init__(
        self,
        *,
        binary: str = "ddcutil",
        timeout: float = _DEFAULT_TIMEOUT,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self._binary = binary
        self._timeout = timeout
        self._runner = runner or subprocess.run
        # ddcutil persists performance stats + cached capabilities under
        # ``$XDG_CACHE_HOME`` (else ``$HOME/.cache``).  The backend service runs
        # with ``ProtectHome=yes``, so ``/home`` is read-only and ddcutil's
        # default cache is unusable — every call then re-probes the slow bus,
        # which is what pushed ``capabilities`` past the timeout.  Point it at
        # a writable dir (under the persistent data dir) by default when one is
        # provided.  ``None`` leaves ddcutil's own default untouched.
        self._cache_dir = str(cache_dir) if cache_dir is not None else None

    def available(self) -> bool:
        return shutil.which(self._binary) is not None

    def detect(self) -> list[DdcMonitor]:
        stdout = self._run(["detect"])
        if stdout is None:
            return []
        return parse_detect(stdout)

    def capabilities(self, display: int) -> DdcCapabilities:
        stdout = self._run(["capabilities", "--display", str(display)])
        if stdout is None:
            return DdcCapabilities(display=display)
        caps = parse_capabilities(stdout, display)
        # Fill current values only for the curated user-facing codes, to keep
        # the number of DDC/CI round-trips low (one getvcp per visible
        # control — brightness, contrast, colour preset, input source).
        from metixel.shared.ddc_types import USER_FACING_CODES

        for feat in caps.features:
            if feat.code not in USER_FACING_CODES:
                continue
            try:
                value = self.get_vcp(display, feat.code)
            except Exception:
                logger.debug(
                    "getvcp 0x%02X failed during capabilities probe",
                    feat.code,
                    exc_info=True,
                )
                continue
            if value is None:
                continue
            feat.current = value.current
            if value.maximum:
                feat.maximum = value.maximum
            if value.feature_type != "unknown":
                feat.feature_type = value.feature_type
            if value.name and (not feat.name or feat.name.lower().startswith("feature ")):
                feat.name = value.name
            enrich_feature(feat)
        return caps

    def get_vcp(self, display: int, code: int) -> DdcVcpValue | None:
        stdout = self._run(
            ["getvcp", f"0x{code:02x}", "--display", str(display)],
            check=False,
        )
        if stdout is None:
            return None
        return parse_getvcp(stdout, code)

    def set_vcp(self, display: int, code: int, value: int) -> None:
        stdout = self._run(
            ["setvcp", f"0x{code:02x}", str(int(value)), "--display", str(display)],
            check=True,
        )
        if stdout is None:
            raise RuntimeError(f"ddcutil setvcp 0x{code:02X}={value} failed for display {display}")

    def reset_factory(self, display: int) -> None:
        """Restore the monitor to factory defaults (VCP 0x04)."""
        stdout = self._run(
            ["setvcp", "0x04", "0x01", "--display", str(display)],
            check=True,
        )
        if stdout is None:
            raise RuntimeError(
                f"ddcutil setvcp 0x04=0x01 (factory reset) failed for display {display}"
            )

    def _run(
        self,
        args: list[str],
        *,
        check: bool = False,
    ) -> str | None:
        # ``--syslog NEVER`` is LOAD-BEARING, not tidiness.  ddcutil writes its
        # diagnostics via openlog()/syslog() — NOT to stderr — so they bypass
        # our subprocess capture entirely and land in the systemd journal.
        # ddcutil prints one ``is_sysfs_reliable_for_busno(<n>)`` line per
        # candidate bus, on every invocation with a cold cache, so a single
        # capabilities probe (detect + capabilities + one getvcp per
        # user-facing code) emitted 18 journal lines.  Measured on a Pi 5
        # (Raspberry Pi OS Trixie, ddcutil 2.2.0-dev): 18 lines → 0.
        #
        # Why this matters beyond log noise: /var/log/journal exists on the
        # image, so journald persists to the SD card.  Metixel's own logs are
        # kept empty at log_level=NONE precisely to spare flash, so routing
        # ddcutil's chatter into a flash-backed journal defeats that.
        # (Log level is also the wrong lever: ddcutil logs these at a level that
        # ERROR — and even NONE — does not suppress; the flag is the fix.)
        #
        # The flag is placed AFTER the subcommand arguments deliberately: all
        # four subcommands accept it there (verified on-device: rc=0), and it
        # keeps cmd[1] as the subcommand so test fakes keyed on cmd[1:] still
        # match.  stdout is byte-identical with and without it (verified via
        # md5 for detect / capabilities / getvcp), so parsing is unaffected.
        # The equivalent environment variable is NOT honoured by ddcutil —
        # only the command-line option works, so it must be passed here rather
        # than set once in the systemd unit.
        cmd = [self._binary, *args, "--syslog", "NEVER"]
        env = self._subprocess_env()
        try:
            result = self._runner(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                env=env,
            )
        except FileNotFoundError:
            logger.warning("%s not found on PATH", self._binary)
            return None
        except subprocess.TimeoutExpired:
            logger.warning("%s timed out: %s", self._binary, " ".join(cmd))
            return None
        except OSError:
            logger.warning("%s failed to start", self._binary, exc_info=True)
            return None

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            logger.debug(
                "%s exited %s: %s — %s",
                self._binary,
                result.returncode,
                " ".join(cmd),
                stderr,
            )
            if check:
                return None
            # Some getvcp codes return non-zero for unsupported features.
            if not (result.stdout or "").strip():
                return None
        return result.stdout or ""

    def _subprocess_env(self) -> dict[str, str] | None:
        """Build the subprocess environment, redirecting ddcutil's cache dir.

        Returns ``None`` (inherit the parent environment unchanged) when no
        ``cache_dir`` was configured.  Otherwise sets ``XDG_CACHE_HOME`` to the
        configured dir (created if needed) so ddcutil persists its performance
        stats + capabilities cache there instead of ``$HOME/.cache`` — which is
        read-only under the backend service's ``ProtectHome=yes`` hardening.
        """
        if self._cache_dir is None:
            return None
        try:
            os.makedirs(self._cache_dir, exist_ok=True)
        except OSError:
            # Read-only/permission failure — fall back to ddcutil's default
            # rather than failing the call outright.
            logger.debug("Could not create ddcutil cache dir %s", self._cache_dir, exc_info=True)
            return None

        env = dict(os.environ)
        env["XDG_CACHE_HOME"] = self._cache_dir
        return env
