#!/usr/bin/env python3
"""Analyze screenshots to verify the virtual matte (letterbox/pillarbox)."""
from PIL import Image

def analyze(path):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    print(f"\n=== {path} ({w}x{h}) ===")
    # Sample corners, edges, and center
    samples = {
        "top-left": (5, 5),
        "top-right": (w-6, 5),
        "bottom-left": (5, h-6),
        "bottom-right": (w-6, h-6),
        "top-center": (w//2, 5),
        "bottom-center": (w//2, h-6),
        "left-center": (5, h//2),
        "right-center": (w-6, h//2),
        "center": (w//2, h//2),
    }
    for name, (x, y) in samples.items():
        print(f"  {name:14s} ({x:4d},{y:4d}): {im.getpixel((x, y))}")

    # Detect black bars: scan a vertical line at center-x and horizontal at center-y
    def is_black(px):
        return all(c < 20 for c in px)

    # Vertical scan (top to bottom) at center-x
    cx = w // 2
    top_black = 0
    for y in range(h):
        if is_black(im.getpixel((cx, y))):
            top_black += 1
        else:
            break
    bot_black = 0
    for y in range(h-1, -1, -1):
        if is_black(im.getpixel((cx, y))):
            bot_black += 1
        else:
            break
    # Horizontal scan (left to right) at center-y
    cy = h // 2
    left_black = 0
    for x in range(w):
        if is_black(im.getpixel((x, cy))):
            left_black += 1
        else:
            break
    right_black = 0
    for x in range(w-1, -1, -1):
        if is_black(im.getpixel((x, cy))):
            right_black += 1
        else:
            break
    print(f"  top bar: {top_black}px, bottom bar: {bot_black}px")
    print(f"  left bar: {left_black}px, right bar: {right_black}px")
    print(f"  => {'LETTERBOX (top/bottom bars)' if top_black > 20 and left_black < 20 else 'PILLARBOX (left/right bars)' if left_black > 20 and top_black < 20 else 'FULLSCREEN or mixed'}")

for p in ["shot_video.png", "shot_32.png", "shot_169.png"]:
    analyze(p)