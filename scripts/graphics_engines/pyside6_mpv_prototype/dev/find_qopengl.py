#!/usr/bin/env python3
"""Find where QOpenGLWidget lives in PySide6."""
import PySide6
print("PySide6", PySide6.__version__)
import PySide6.QtOpenGLWidgets as ow
print("QtOpenGLWidgets module:", ow)
print("QOpenGLWidget:", hasattr(ow, "QOpenGLWidget"))
from PySide6.QtOpenGLWidgets import QOpenGLWidget
print("imported QOpenGLWidget from QtOpenGLWidgets OK")