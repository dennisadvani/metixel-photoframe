#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Load Frame.qml and report whether it actually resolves.

Static checking cannot validate a QML scene. A missing module, a mistyped property
name or a bad binding only fails when the engine instantiates it, and the failure
mode on a frame is the worst kind: a window that opens and draws nothing, with the
reason buried in a warning nobody is watching. This loads the scene offscreen and
prints every warning, so a scene error is caught while there is still a console.

Offscreen on purpose: no compositor, no DRM, no GPU groups — so it can run over
plain ssh on a Pi, and on a dev machine that has PySide6 but no display.

Usage:
    QT_QPA_PLATFORM=offscreen python3 _smoke_qml_scene.py [path/to/Frame.qml]

Exit codes: 0 clean, 1 loaded-with-warnings or failed, 2 scene file missing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SCENE_RELATIVE = Path("src") / "metixel" / "display" / "qml" / "Frame.qml"


def _default_scene() -> Path:
    """Locate ``Frame.qml``, tolerating being copied out of the repo.

    The point of this script is to be scp'd to a frame and run over plain ssh —
    where it will NOT sit at ``<repo>/scripts/dev/``.  Walking up for the scene
    keeps that working.  A fixed ``parents[2]`` raised IndexError at *import*
    time, before ``argv`` was read, so passing an explicit path could not rescue
    it either.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / _SCENE_RELATIVE
        if candidate.exists():
            return candidate
    # Nothing found: return the repo-shaped guess so the caller reports
    # "MISSING: <path>" and exits 2 rather than raising.
    return here.parent / _SCENE_RELATIVE


DEFAULT = _default_scene()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    try:
        from PySide6.QtCore import qVersion
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
    except ImportError as exc:
        print(f"SKIP: PySide6 with Qt Quick is unavailable ({exc})")
        return 2

    scene = Path(args[0]) if args else DEFAULT
    print(f"scene  : {scene}")
    if not scene.exists():
        print(f"MISSING: {scene}")
        return 2

    warnings: list[str] = []
    app = QGuiApplication([])
    engine = QQmlApplicationEngine()
    engine.warnings.connect(lambda items: warnings.extend(item.toString() for item in items))
    engine.load(scene.as_uri())

    roots = engine.rootObjects()
    print(f"qt     : {qVersion()}")
    for warning in warnings:
        print(f"WARN   : {warning}")
    print(f"roots  : {len(roots)}")

    # Report the layer order the engine actually built, not the order we intended.
    # QML stacks by declaration order, and the whole video-hole removal depends on
    # the VideoOutput landing between the artwork poster and the ring layers — a
    # silent reorder would put the video behind an opaque mat and it would look like
    # a video-decoding failure.
    if roots:
        window = roots[0]
        names = [
            child.objectName()
            for child in window.children()
            if hasattr(child, "objectName") and child.objectName()
        ]
        print(f"layers : {' -> '.join(names)}")

    _ = app

    if not roots:
        print("VERDICT: FAILED — the scene did not instantiate")
        return 1
    if warnings:
        print("VERDICT: LOADED WITH WARNINGS — fix these before trusting it on a frame")
        return 1
    print("VERDICT: OK — scene instantiated with no warnings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
