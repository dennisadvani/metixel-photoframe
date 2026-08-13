#!/usr/bin/env python3
"""Bump the version in src/metixel/__init__.py.

Usage:
    python scripts/bump_version.py                # bump patch (0.1.0 → 0.1.1)
    python scripts/bump_version.py --minor        # bump minor (0.1.0 → 0.2.0)
    python scripts/bump_version.py --major        # bump major (0.1.0 → 1.0.0)
    python scripts/bump_version.py --beta         # set pre-release beta (0.1.3 → 0.1.3-beta.1)
    python scripts/bump_version.py --beta 3       # set specific beta number
    python scripts/bump_version.py --rc           # set pre-release rc (0.1.3 → 0.1.3-rc.1)
    python scripts/bump_version.py --alpha        # set pre-release alpha
    python scripts/bump_version.py --release      # strip pre-release (0.1.3-beta.1 → 0.1.3)
    python scripts/bump_version.py --set 0.2.0    # set an exact version
    python scripts/bump_version.py --minor --beta 1  # bump minor + set beta
    python scripts/bump_version.py --dry-run      # print new version, don't write
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
INIT_FILE = REPO_ROOT / "src" / "metixel" / "__init__.py"

# Matches the same semver shape as UpdateManager._SEMVER_RE:
#   "0.1.3"  or  "0.2.0-beta.1"  or  "1.0.0-rc.2"
VERSION_RE = re.compile(
    r'^__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)(?:[-.](beta|rc|alpha|pre)\.?(\d+))?"$',
    re.MULTILINE,
)

PRE_LABELS = {"beta", "rc", "alpha", "pre"}


def read_version() -> dict[str, Any]:
    """Read the current version from src/metixel/__init__.py.

    Returns a dict with keys: major, minor, patch, pre_label (str|None), pre_num (int).
    """
    text = INIT_FILE.read_text(encoding="utf-8")
    m = VERSION_RE.search(text)
    if not m:
        raise SystemExit(
            f"ERROR: Could not parse __version__ in {INIT_FILE}\n"
            f"       Expected format: major.minor.patch  or  major.minor.patch-label.N"
        )
    return {
        "major": int(m.group(1)),
        "minor": int(m.group(2)),
        "patch": int(m.group(3)),
        "pre_label": m.group(4),  # "beta", "rc", "alpha", "pre", or None
        "pre_num": int(m.group(5)) if m.group(5) else 0,
    }


def _format_version(ver: dict[str, Any]) -> str:
    """Format a version dict back to a string like ``0.2.0-beta.1``."""
    base = f"{ver['major']}.{ver['minor']}.{ver['patch']}"
    if ver.get("pre_label"):
        base += f"-{ver['pre_label']}.{ver['pre_num']}"
    return base


def write_version(ver: dict[str, Any]) -> str:
    """Write the version dict back to src/metixel/__init__.py.

    Returns the formatted version string.
    """
    new_ver = _format_version(ver)
    text = INIT_FILE.read_text(encoding="utf-8")
    new_text = VERSION_RE.sub(f'__version__ = "{new_ver}"', text)
    INIT_FILE.write_text(new_text, encoding="utf-8")
    return new_ver


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bump Metixel Photoframe version",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
        "  bump_version.py                  # 0.1.3 → 0.1.4\n"
        "  bump_version.py --minor           # 0.1.3 → 0.2.0\n"
        "  bump_version.py --major           # 0.1.3 → 1.0.0\n"
        "  bump_version.py --beta            # 0.1.3 → 0.1.3-beta.1\n"
        "  bump_version.py --beta 3          # 0.1.3 → 0.1.3-beta.3\n"
        "  bump_version.py --rc              # 0.1.3 → 0.1.3-rc.1\n"
        "  bump_version.py --release         # 0.1.3-beta.1 → 0.1.3\n"
        "  bump_version.py --set 0.2.0-beta.1\n"
        "  bump_version.py --minor --beta 1  # 0.1.3 → 0.2.0-beta.1",
    )

    # -- Bump type (mutually exclusive) ------------------------------------
    bump_group = parser.add_mutually_exclusive_group()
    bump_group.add_argument(
        "--major", action="store_true", help="Bump major version, reset lower segments"
    )
    bump_group.add_argument("--minor", action="store_true", help="Bump minor version, reset patch")

    # -- Pre-release channel (mutually exclusive with each other) -----------
    pre_group = parser.add_mutually_exclusive_group()
    pre_group.add_argument(
        "--beta",
        nargs="?",
        type=int,
        const=-1,
        default=None,
        metavar="N",
        help=(
            "Set pre-release to beta.N (auto-increments if already beta; defaults to 1 if omitted)"
        ),
    )
    pre_group.add_argument(
        "--rc",
        nargs="?",
        type=int,
        const=-1,
        default=None,
        metavar="N",
        help="Set pre-release to rc.N (auto-increments if already rc)",
    )
    pre_group.add_argument(
        "--alpha",
        nargs="?",
        type=int,
        const=-1,
        default=None,
        metavar="N",
        help="Set pre-release to alpha.N (auto-increments if already alpha)",
    )
    pre_group.add_argument(
        "--pre",
        nargs="?",
        type=int,
        const=-1,
        default=None,
        metavar="N",
        help="Set pre-release to pre.N (auto-increments if already pre)",
    )
    pre_group.add_argument(
        "--release",
        action="store_true",
        help="Strip any pre-release suffix (release version)",
    )

    # -- Explicit set ------------------------------------------------------
    parser.add_argument(
        "--set",
        dest="set_version",
        metavar="VERSION",
        help="Set an exact version string (e.g. 0.2.0-beta.1)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print new version without writing",
    )

    args = parser.parse_args()

    # ------------------------------------------------------------------
    # --set overrides everything
    if args.set_version:
        raw = args.set_version.strip().lstrip("v")
        # Parse with the same semver pattern used by UpdateManager
        semver_re = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-.](beta|rc|alpha|pre)\.?(\d+))?$")
        sm = semver_re.match(raw)
        if not sm:
            raise SystemExit(
                f"ERROR: '{args.set_version}' is not a valid semver.\n"
                f"       Expected: major.minor.patch  or  major.minor.patch-label.N"
            )
        new_ver: dict[str, Any] = {
            "major": int(sm.group(1)),
            "minor": int(sm.group(2)),
            "patch": int(sm.group(3)),
            "pre_label": sm.group(4),
            "pre_num": int(sm.group(5)) if sm.group(5) else 0,
        }
        if args.dry_run:
            print(_format_version(new_ver))
        else:
            result = write_version(new_ver)
            print(f"Set version: {result}")
            print(f"  File: {INIT_FILE}")
        return

    # ------------------------------------------------------------------
    ver = read_version()

    # -- Apply bump -----------------------------------------------------
    if args.major:
        ver["major"] += 1
        ver["minor"] = 0
        ver["patch"] = 0
    elif args.minor:
        ver["minor"] += 1
        ver["patch"] = 0
    else:
        ver["patch"] += 1  # default: bump patch

    # -- Apply pre-release changes --------------------------------------
    # Determine which pre-release flag (if any) was used
    pre_flag: str | None = None
    pre_value: int | None = None
    if args.beta is not None:
        pre_flag, pre_value = "beta", args.beta
    elif args.rc is not None:
        pre_flag, pre_value = "rc", args.rc
    elif args.alpha is not None:
        pre_flag, pre_value = "alpha", args.alpha
    elif args.pre is not None:
        pre_flag, pre_value = "pre", args.pre

    if args.release:
        # Strip pre-release entirely
        ver["pre_label"] = None
        ver["pre_num"] = 0
    elif pre_flag is not None:
        # When bumping a segment AND adding a pre-release, reset pre_num
        # unless explicitly specified
        if pre_value == -1:
            # const=-1 means no explicit N was given (auto)
            if ver["pre_label"] == pre_flag:
                # Same channel: auto-increment
                ver["pre_num"] += 1
            else:
                # Different channel or first pre-release: start at 1
                ver["pre_num"] = 1
            ver["pre_label"] = pre_flag
        else:
            # Explicit N given (e.g. --beta 3)
            ver["pre_label"] = pre_flag
            ver["pre_num"] = pre_value
    else:
        # No pre-release flag — if a bump was applied (major/minor), strip pre-release.
        # For a plain patch bump, preserve the existing pre-release label.
        if args.major or args.minor:
            ver["pre_label"] = None
            ver["pre_num"] = 0
        # else: preserve whatever pre_label was there

    # ------------------------------------------------------------------
    formatted = _format_version(ver)

    if args.dry_run:
        print(formatted)
    else:
        result = write_version(ver)
        print(f"Bumped version: {result}")
        print(f"  File: {INIT_FILE}")


if __name__ == "__main__":
    main()
