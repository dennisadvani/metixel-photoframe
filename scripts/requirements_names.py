#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Parse a requirements file into bare package names.

Shared by ``scripts/update.sh`` steps 3 (remove obsolete packages) and 8 (record
the new manifest).  It was previously duplicated inline in both heredocs, which
meant a fix to one would silently miss the other.

Usage:
    requirements_names.py <requirements-file> [<requirements-file> ...]

Prints one name per line, deduplicated and sorted.  Exit status is always 0;
callers treat unreadable/missing files as an empty set.

**Encoding is pinned to UTF-8 deliberately.**  The requirements files contain
UTF-8 in their comments (em-dashes), and this script runs inside an OTA launched
by ``systemd-run``, which provides a minimal environment with no locale set.  On
Linux ``LC_ALL=C`` makes ``open()`` default to ASCII, which cannot decode those
bytes.  Pinning the encoding removes the dependency on the ambient locale
entirely, so the parse result is identical wherever it runs.  Do not remove the
explicit ``encoding=`` argument.
"""

from __future__ import annotations

import os
import sys

# Characters that terminate the package name in a requirement specifier:
#   "pkg>=1.2" -> "pkg", "pkg[extra]" -> "pkg", "pkg; marker" -> "pkg"
_TERMINATORS = "=<>~! \t"


def package_name(line: str) -> str:
    """Extract the bare package name from one requirements line.

    Returns an empty string for blank lines and comments.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return ""
    # Environment markers come last and contain spaces, so cut there first.
    name = stripped.split(";", 1)[0].strip()
    # Extras: "pillow-heif[extra]" -> "pillow-heif"
    name = name.split("[", 1)[0].strip()
    # Version specifiers: "numpy>=1.24" -> "numpy"
    for ch in _TERMINATORS:
        name = name.split(ch, 1)[0].strip()
    return name


def names(paths: list[str]) -> list[str]:
    """Return the sorted, deduplicated package names across ``paths``."""
    found: set[str] = set()
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                name = package_name(line)
                if name:
                    found.add(name)
    return sorted(found)


def main(argv: list[str]) -> int:
    for name in names(argv[1:]):
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
