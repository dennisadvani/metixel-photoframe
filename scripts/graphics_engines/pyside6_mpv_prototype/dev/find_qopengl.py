#!/usr/bin/env python3
"""Find where QOpenGLWidget lives in PySide6."""

import PySide6

print("PySide6", PySide6.__version__)
import PySide6.QtOpenGLWidgets as QtOpenGLWidgets  # noqa: E402  (print version first)

print("QtOpenGLWidgets module:", QtOpenGLWidgets)
print("QOpenGLWidget:", hasattr(QtOpenGLWidgets, "QOpenGLWidget"))
print("imported QOpenGLWidget from QtOpenGLWidgets OK")
