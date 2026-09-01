#!/usr/bin/env python3
"""Test wid-embedding with stable native window + vo=gpu."""
import faulthandler
import locale
import sys
faulthandler.enable()

from PySide6.QtWidgets import QApplication, QMainWindow, QWidget
from PySide6.QtCore import Qt
import mpv

class MetixelPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PySide6 + MPV Frame")
        self.resize(1920, 1080)
        self.container = QWidget(self)
        self.setCentralWidget(self.container)
        # Force native window creation and keep it stable
        self.container.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.container.winId()
        print("container winId:", self.container.winId(), flush=True)
        self.player = mpv.MPV(
            wid=str(int(self.container.winId())),
            vo='gpu',
            gpu_context='x11egl',
            hwdec='auto',
            log_handler=print,
        )
        print("MPV created OK", flush=True)
        self.player.play('/opt/metixel/data/media/sample_media/13131508_1920_1080_24fps.mp4')
        print("play() called", flush=True)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    print("QApplication created", flush=True)
    locale.setlocale(locale.LC_NUMERIC, 'C')
    print("LC_NUMERIC reset to C after app", flush=True)
    window = MetixelPlayer()
    window.show()
    print("window shown, entering event loop", flush=True)
    from PySide6.QtCore import QTimer
    QTimer.singleShot(10000, app.quit)
    sys.exit(app.exec())