#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Why is the artwork rectangle black?  Three modes, one capture each.

The software render path fills its CPU buffer correctly (verified: 1.04 M lit
bytes through the production marshalling) and the artwork hole IS being revealed,
but the rectangle on screen is black.  Two things were then ruled out:

  * ``gl``       — raw GL clear renders (100% green).
  * ``qpainter`` — ``QPainter`` on a QOpenGLWidget renders, including
                   ``drawImage`` of a ``bytearray``-backed ``QImage`` (49% red /
                   49% blue).

Both of those used a *top-level* GL widget.  Production is different in one
respect: the GL widget is a CHILD, with a plain ``QWidget`` sibling raised above
it that leaves a rectangle unpainted so the child shows through.  That is the
mechanism this module tests:

  * ``stacked`` — the production structure: GL child underneath, canvas sibling
                  raised on top with a hole.  A working hole shows RED/BLUE; a
                  broken one shows BLACK.

Run under cage:  cage -d -- python3 _probe_qpainter_on_glwidget.py --mode stacked
"""

from __future__ import annotations

import argparse
import ctypes
import sys

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication, QWidget

SURROUND = QColor(255, 255, 0)  # yellow: the canvas's "everything but the hole"


class VideoChild(QOpenGLWidget):
    """Stands in for MpvRenderWidget: a QOpenGLWidget child painted with QPainter."""

    def __init__(self, mode: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._mode = mode
        self.paint_count = 0
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        width, height = 320, 200
        self._pixels = bytearray(width * height * 4)
        self._backing = (ctypes.c_char * len(self._pixels)).from_buffer(self._pixels)
        self._address = ctypes.addressof(self._backing)
        self._image = QImage(self._pixels, width, height, width * 4, QImage.Format.Format_RGBX8888)
        for i in range(0, len(self._pixels), 4):
            self._pixels[i + 2] = 255  # blue

    def paintGL(self) -> None:  # noqa: N802 - Qt naming
        self.paint_count += 1
        if self._mode == "gl":
            from PySide6.QtGui import QOpenGLFunctions

            functions = QOpenGLFunctions()
            functions.initializeOpenGLFunctions()
            functions.glClearColor(0.0, 1.0, 0.0, 1.0)
            functions.glClear(0x00004000)
            return
        painter = QPainter(self)
        if not painter.isActive():
            print(f"!! QPainter isActive=False (paint #{self.paint_count})")
            return
        painter.fillRect(QRect(0, 0, self.width() // 2, self.height()), QColor(255, 0, 0))
        painter.drawImage(
            QRect(self.width() // 2, 0, self.width() // 2, self.height()), self._image
        )
        painter.end()


class CanvasSibling(QWidget):
    """Stands in for FrameCanvas: paints a surround and LEAVES THE HOLE UNPAINTED."""

    def __init__(self, hole: QRect, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hole = hole
        self.paint_count = 0
        # Deliberately NOT opaque, exactly like the real canvas in video mode:
        # claiming opaque while leaving a region unpainted shows undefined content.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    def paintEvent(self, _event: object) -> None:  # noqa: N802 - Qt naming
        self.paint_count += 1
        painter = QPainter(self)
        full = self.rect()
        hole = self._hole
        # Four bands around the hole.  Simpler and more predictable than a path
        # with a fill rule, and it leaves the hole genuinely untouched.
        bands = [
            QRect(full.left(), full.top(), full.width(), hole.top() - full.top()),
            QRect(full.left(), hole.bottom() + 1, full.width(), full.bottom() - hole.bottom()),
            QRect(full.left(), hole.top(), hole.left() - full.left(), hole.height()),
            QRect(hole.right() + 1, hole.top(), full.right() - hole.right(), hole.height()),
        ]
        for band in bands:
            if band.width() > 0 and band.height() > 0:
                painter.fillRect(band, SURROUND)
        painter.end()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["gl", "qpainter", "stacked"], default="stacked")
    parser.add_argument("--seconds", type=float, default=12.0)
    args = parser.parse_args()

    app = QApplication(sys.argv)

    if args.mode != "stacked":
        widget = VideoChild(args.mode)
        widget.setWindowTitle(f"paint probe [{args.mode}]")
        widget.showFullScreen()
        widget.update()

        def report_simple() -> None:
            print(
                f"mode={args.mode} platform={app.platformName()} "
                f"size={widget.width()}x{widget.height()} paints={widget.paint_count}"
            )
            app.quit()

        QTimer.singleShot(int(args.seconds * 1000), report_simple)
        return app.exec()

    container = QWidget()
    container.setStyleSheet("background-color: black;")
    container.resize(1920, 1200)

    child = VideoChild("qpainter", container)
    child.setGeometry(0, 0, 1920, 1200)

    # A portrait-ish hole, like the artwork rectangle a portrait video produces.
    hole = QRect(int(1920 * 0.30), int(1200 * 0.10), int(1920 * 0.40), int(1200 * 0.80))
    canvas = CanvasSibling(hole, container)
    canvas.setGeometry(0, 0, 1920, 1200)
    canvas.raise_()  # the canvas is ABOVE the video widget, as in production

    container.setWindowTitle("paint probe [stacked]")
    container.showFullScreen()

    def report() -> None:
        print(
            f"mode=stacked platform={app.platformName()} "
            f"container={container.width()}x{container.height()} hole={hole.getRect()} "
            f"child_paints={child.paint_count} canvas_paints={canvas.paint_count}"
        )
        app.quit()

    QTimer.singleShot(int(args.seconds * 1000), report)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
