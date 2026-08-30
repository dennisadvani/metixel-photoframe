#!/usr/bin/env python3
"""Verify PySide6 + QtOpenGL imports work on the Pi."""
import PySide6
print("PySide6", PySide6.__version__)
from PySide6.QtOpenGL import QOpenGLFramebufferObject, QOpenGLTexture
from PySide6.QtWidgets import QOpenGLWidget
print("QtOpenGL imports OK")
from PySide6.QtGui import QGuiApplication, QOpenGLContext
print("QtGui imports OK")