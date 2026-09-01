#!/usr/bin/env python3
"""Analyze screenshots to verify the virtual matte (letterbox/pillarbox)."""
from PIL import Image

def analyze(path):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    print(f"\n=== {path} ({w}x{h}) ===")

    def is_black(px):
        return all(c < 15 for c in px)

    # Find the bounding box of non-black content
    # Scan rows from top until we find a row with non-black pixels
    def row_has_content(y):
        # sample across the row
        for x in range(0, w, 8):
            if not is_black(im.getpixel((x, y))):
                return True
        return False

    def col_has_content(x):
        for y in range(0, h, 8):
            if not is_black(im.getpixel((x, y))):
                return True
        return False

    top = 0
    while top < h and not row_has_content(top):
        top += 1
    bottom = h - 1
    while bottom > top and not row_has_content(bottom):
        bottom -= 1
    left = 0
    while left < w and not col_has_content(left):
        left += 1
    right = w - 1
    while right > left and not col_has_content(right):
        right -= 1

    content_w = right - left + 1
    content_h = bottom - top + 1
    print(f"  content bbox: x=[{left},{right}] y=[{top},{bottom}]")
    print(f"  content size: {content_w}x{content_h} (ratio {content_w/content_h:.3f})")
    print(f"  top bar: {top}px, bottom bar: {h-1-bottom}px")
    print(f"  left bar: {left}px, right bar: {w-1-right}px")

    if top > 20 and left <= 20:
        print("  => LETTERBOX (top/bottom bars) — correct for wider-than-display media")
    elif left > 20 and top <= 20:
        print("  => PILLARBOX (left/right bars) — correct for narrower-than-display media")
    elif top > 20 and left > 20:
        print("  => BOTH bars — media aspect matches display, or scaling issue")
    else:
        print("  => FULLSCREEN (no bars)")

for p in ["shot_video.png", "shot_32.png", "shot_169.png"]:
    analyze(p)