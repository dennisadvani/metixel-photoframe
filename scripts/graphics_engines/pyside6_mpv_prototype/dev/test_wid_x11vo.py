#!/usr/bin/env python3
"""Test wid-embedding with vo=x11 instead of vo=gpu."""

import faulthandler
import locale
import sys

faulthandler.enable()

import mpv
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget


class MetixelPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PySide6 + MPV Frame")
        self.resize(1920, 1080)
        self.container = QWidget(self)
        self.setCentralWidget(self.container)
        print("container winId:", self.container.winId(), flush=True)
        self.player = mpv.MPV(
            wid=str(int(self.container.winId())),
            vo="x11",
            hwdec="auto",
            log_handler=print,
        )
        print("MPV created OK", flush=True)
        self.player.play("/opt/metixel/data/media/sample_media/13131508_1920_1080_24fps.mp4")
        print("play() called", flush=True)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    print("QApplication created", flush=True)
    locale.setlocale(locale.LC_NUMERIC, "C")
    print("LC_NUMERIC reset to C after app", flush=True)
    window = MetixelPlayer()
    window.show()
    print("window shown, entering event loop", flush=True)
    from PySide6.QtCore import QTimer

    QTimer.singleShot(10000, app.quit)
    sys.exit(app.exec())
