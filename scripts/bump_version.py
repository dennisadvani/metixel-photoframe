#!/usr/bin/env python3
"""Bump the patch version in metixel/__init__.py.

Usage:
    python scripts/bump_version.py           # bump patch (0.1.0 → 0.1.1)
    python scripts/bump_version.py --minor   # bump minor (0.1.0 → 0.2.0)
    python scripts/bump_version.py --major   # bump major (0.1.0 → 1.0.0)
    python scripts/bump_version.py --dry-run # print new version, don't write
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INIT_FILE = REPO_ROOT / "metixel" / "__init__.py"
VERSION_RE = re.compile(r'^__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"$', re.MULTILINE)


def read_version() -> tuple[int, int, int]:
    text = INIT_FILE.read_text(encoding="utf-8")
    m = VERSION_RE.search(text)
    if not m:
        raise SystemExit(f"ERROR: Could not find __version__ in {INIT_FILE}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def write_version(major: int, minor: int, patch: int) -> str:
    new_ver = f"{major}.{minor}.{patch}"
    text = INIT_FILE.read_text(encoding="utf-8")
    new_text = VERSION_RE.sub(f'__version__ = "{new_ver}"', text)
    INIT_FILE.write_text(new_text, encoding="utf-8")
    return new_ver


def main() -> None:
    parser = argparse.ArgumentParser(description="Bump Metixel Photoframe version")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--major", action="store_true", help="Bump major version")
    group.add_argument("--minor", action="store_true", help="Bump minor version")
    parser.add_argument("--dry-run", action="store_true", help="Print new version without writing")
    args = parser.parse_args()

    major, minor, patch = read_version()

    if args.major:
        major += 1
        minor = 0
        patch = 0
    elif args.minor:
        minor += 1
        patch = 0
    else:
        patch += 1  # default: bump patch

    if args.dry_run:
        print(f"{major}.{minor}.{patch}")
    else:
        new_ver = write_version(major, minor, patch)
        print(f"Bumped version: {new_ver}")
        print(f"  File: {INIT_FILE}")


if __name__ == "__main__":
    main()
