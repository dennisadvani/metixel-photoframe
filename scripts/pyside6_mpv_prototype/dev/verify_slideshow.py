#!/usr/bin/env python3
"""Verify slideshow.py imports cleanly on the Pi."""
import sys
sys.path.insert(0, "/tmp/metixel-pyside-mpv")
import slideshow
print("slideshow imports OK")
print("MpvRenderContext:", hasattr(slideshow, "MpvRenderContext"))
print("MpvRenderWidget:", hasattr(slideshow, "MpvRenderWidget"))
print("SlideshowWindow:", hasattr(slideshow, "SlideshowWindow"))