# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""System Message Layer — pop-up notifications with slide-in animation.

Renders above the slideshow at z_base=0.01.  Messages slide in from the
right edge, display for a configurable duration, then slide out.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from metixel.display.backend import DisplayBackend
from metixel.frontend.overlay.layer import OverlayLayer

logger = logging.getLogger(__name__)

# -- Layout constants --------------------------------------------------------
MSG_WIDTH = 500            # 60% of original
MSG_MARGIN = 24
MSG_PADDING = 20
MSG_ACCENT = 5
MSG_GAP = 14
MSG_ICON_SIZE = 28
MSG_TITLE_SIZE = 18
MSG_BODY_SIZE = 14
MSG_BG = (0.02, 0.02, 0.04)
MSG_BG_ALPHA = 0.92
MSG_TEXT_ALPHA = 0.95

# Accent colour — dark red
ACCENT_COLOR = (0.8, 0.0, 0.0)

SLIDE_IN_MS = 400
SLIDE_OUT_MS = 300
MAX_VISIBLE = 5
CLEANUP_INTERVAL = 2.0

SEVERITY_COLORS = {
    "info": (0.20, 0.55, 0.90),
    "warning": (0.95, 0.60, 0.10),
    "error": (0.90, 0.20, 0.20),
    "success": (0.20, 0.75, 0.35),
}
SEVERITY_ICONS = {"info": "i", "warning": "!", "error": "x", "success": "v"}


def _wrap_text(text: str, chars_per_line: int) -> list[str]:
    """Split text into lines at word boundaries, max *chars_per_line* per line."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip() if current else word
        if len(test) <= chars_per_line:
            current = test
        else:
            if current:
                lines.append(current)
            current = word if len(word) <= chars_per_line else word[:chars_per_line]
    if current:
        lines.append(current)
    return lines if lines else [text[:chars_per_line]]


class _Message:
    """Internal message state."""
    __slots__ = ("id", "icon", "title", "body", "severity", "duration",
                 "created_at", "state", "_anim_start", "_from_x", "_to_x",
                 "_x", "_alpha", "_y")
    def __init__(self, msg_id, icon, title, body, severity, duration):
        self.id = msg_id
        self.icon = icon
        self.title = title
        self.body = body
        self.severity = severity
        self.duration = duration
        self.created_at = time.monotonic()
        self.state = "hidden"  # hidden, sliding_in, visible, sliding_out, done
        self._anim_start = 0.0
        self._from_x = 0.0
        self._to_x = 0.0
        self._x = 0.0
        self._alpha = 0.0
        self._y = 0.0

    @property
    def active(self) -> bool:
        return self.state in ("sliding_in", "visible", "sliding_out")


class MessageLayer(OverlayLayer):
    """System message pop-ups that slide in from the right edge."""

    def __init__(self) -> None:
        super().__init__("messages", OverlayLayer.Z_MESSAGES)
        self._msgs: list[_Message] = []
        self._lock = threading.Lock()
        self._next_id = 0
        self._last_cleanup = 0.0
        self._video_playing = False

    # -- Public API (thread-safe) -------------------------------------------

    def show(self, title: str = "", body: str = "",
             severity: str = "info", duration: float = 5.0,
             icon: str = "") -> str:
        """Queue a message. Returns the message ID."""
        if icon == "":
            icon = SEVERITY_ICONS.get(severity, SEVERITY_ICONS["info"])
        self._next_id += 1
        msg_id = f"m{self._next_id}"
        msg = _Message(msg_id, icon, title, body, severity, duration)
        with self._lock:
            active = [m for m in self._msgs if m.active]
            if len(active) >= MAX_VISIBLE and active:
                active[0].state = "sliding_out"
                active[0]._anim_start = time.monotonic()
                active[0]._from_x = active[0]._x
            self._msgs.append(msg)
        logger.info("Message queued: id=%s severity=%s title=%r", msg_id, severity, title)
        return msg_id

    def dismiss(self, msg_id: str) -> bool:
        with self._lock:
            for m in self._msgs:
                if m.id == msg_id and m.active:
                    self._start_dismiss(m)
                    return True
        return False

    def dismiss_all(self) -> int:
        count = 0
        with self._lock:
            for m in self._msgs:
                if m.active:
                    self._start_dismiss(m)
                    count += 1
        return count

    def _start_dismiss(self, m: _Message) -> None:
        m.state = "sliding_out"
        m._anim_start = time.monotonic()
        m._from_x = m._x

    # -- OverlayLayer interface ---------------------------------------------

    def update(self, shared_state: dict[str, Any] | None = None) -> None:
        self._video_playing = (shared_state or {}).get("video_playing", False)
        now = time.monotonic()
        with self._lock:
            for m in self._msgs:
                self._tick(m, now)

        if now - self._last_cleanup >= CLEANUP_INTERVAL:
            with self._lock:
                self._msgs = [m for m in self._msgs if m.state != "done"]
            self._last_cleanup = now

    def _tick(self, m: _Message, now: float) -> None:
        if m.state == "hidden":
            m.state = "sliding_in"
            m._anim_start = now
            m._from_x = 2000  # off-screen right (will be adjusted in draw)
            m._to_x = 0
            m._alpha = 0.0
        elif m.state == "sliding_in":
            t = (now - m._anim_start) * 1000.0 / SLIDE_IN_MS
            if t >= 1.0:
                m.state = "visible"
                m._alpha = 1.0
                m._anim_start = now
            else:
                # ease_out_cubic
                et = 1.0 - (1.0 - t) ** 3
                m._alpha = min(1.0, t / 0.5)
        elif m.state == "visible":
            if self._video_playing:
                # Reset timer while VLC covers the screen
                m._anim_start = now
            elif m.duration > 0 and (now - m._anim_start) >= m.duration:
                self._start_dismiss(m)
        elif m.state == "sliding_out":
            t = (now - m._anim_start) * 1000.0 / SLIDE_OUT_MS
            if t >= 1.0:
                m.state = "done"
                m._alpha = 0.0

    def draw(self, backend: DisplayBackend) -> None:
        self.reset_z()
        # Lazy-init textures (created once, reused every frame)
        if not hasattr(self, '_tex_bg'):
            import numpy as np
            bg_arr = np.ones((1, 1, 3), dtype=np.uint8)
            bg_arr[0, 0] = tuple(int(c * 255) for c in MSG_BG)
            self._tex_bg = backend.load_texture(bg_arr)
            accent_arr = np.ones((1, 1, 3), dtype=np.uint8)
            accent_arr[0, 0] = tuple(int(c * 255) for c in ACCENT_COLOR)
            self._tex_accent = backend.load_texture(accent_arr)
        bw = backend.width
        mw = MSG_WIDTH
        margin = MSG_MARGIN
        target_x = bw - mw - margin

        with self._lock:
            for idx, m in enumerate(self._msgs):
                if not m.active:
                    continue
                self._draw_one(backend, m, idx, bw, target_x, margin)

    @staticmethod
    def _msg_height(m: _Message) -> int:
        """Compute the pixel height needed for a message based on its content."""
        # Base padding (top + bottom)
        h = MSG_PADDING * 2
        # Title
        if m.title:
            h += MSG_TITLE_SIZE + 4
        # Body — count wrapped lines, cap at 5
        if m.body:
            char_w = MSG_BODY_SIZE * 0.55
            avail_w = MSG_WIDTH - MSG_ACCENT - MSG_PADDING - MSG_ICON_SIZE - MSG_PADDING - 10
            chars_per = max(20, int(avail_w / char_w))
            lines = _wrap_text(m.body, chars_per)
            body_lines = min(len(lines), 5)
            h += body_lines * (MSG_BODY_SIZE + 4)
        # Ensure minimum height
        return max(h, 60)

    def _draw_one(self, backend, m, idx, bw, target_x, margin):
        # Compute message x position (animated)
        if m.state == "sliding_in":
            t = (time.monotonic() - m._anim_start) * 1000.0 / SLIDE_IN_MS
            et = 1.0 - (1.0 - min(t, 1.0)) ** 3
            m._x = bw + margin - (bw + margin - target_x) * et
        elif m.state == "sliding_out":
            t = (time.monotonic() - m._anim_start) * 1000.0 / SLIDE_OUT_MS
            et = min(t, 1.0) ** 3
            m._x = m._from_x + (bw + margin - m._from_x) * et
        else:
            m._x = target_x

        # Stack position — compute message height from content
        mh = self._msg_height(m)
        m._y = margin + idx * (mh + MSG_GAP)

        x, y, alpha = m._x, m._y, m._alpha
        if alpha <= 0.01:
            return

        # 1. Background — use draw_image (same as slideshow, correct colours)
        backend.draw_image(self._tex_bg, x, y, MSG_WIDTH, mh,
                           alpha=MSG_BG_ALPHA * alpha,
                           z=self.next_z())

        # 2. Accent bar — use draw_image (bypasses draw_rect colour bug)
        backend.draw_image(self._tex_accent, x, y, MSG_ACCENT, mh,
                           alpha=1.0 * alpha,
                           z=self.next_z())

        # 3. Icon — vertically centered in the box
        icon_x = int(x + MSG_ACCENT + MSG_PADDING)
        icon_y = int(y + 8)
        backend.draw_text(m.icon, icon_x, icon_y,
                          font_size=MSG_ICON_SIZE,
                          color=(1, 1, 1, MSG_TEXT_ALPHA * alpha),
                          z=self.next_z())

        # 4. Title — positioned higher, next to icon
        text_x = int(icon_x + MSG_ICON_SIZE + MSG_PADDING)
        if m.title:
            backend.draw_text(m.title, text_x,
                              int(y + 6),
                              font_size=MSG_TITLE_SIZE,
                              color=(1, 1, 1, MSG_TEXT_ALPHA * alpha),
                              z=self.next_z())

        # 5. Body — wrapping support
        if m.body:
            # Estimate chars per line based on available pixel width
            body_start_y = int(y + MSG_TITLE_SIZE + 10) if m.title else int(y + 6)
            char_w = MSG_BODY_SIZE * 0.55
            avail_w = MSG_WIDTH - MSG_ACCENT - MSG_PADDING - MSG_ICON_SIZE - MSG_PADDING - 10
            chars_per = max(20, int(avail_w / char_w))
            lines = _wrap_text(m.body, chars_per)
            for li, line in enumerate(lines[:5]):  # max 5 lines
                backend.draw_text(line, text_x,
                                  body_start_y + li * (MSG_BODY_SIZE + 4),
                                  font_size=MSG_BODY_SIZE,
                                  color=(0.78, 0.78, 0.82, MSG_TEXT_ALPHA * 0.9 * alpha),
                                  z=self.next_z())
