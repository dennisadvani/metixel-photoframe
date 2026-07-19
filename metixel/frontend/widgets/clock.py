# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Clock widget — digital clock overlay with translucent background."""

from __future__ import annotations

import logging
import time
from typing import Any

from metixel.display.backend import DisplayBackend
from metixel.frontend.widgets.base import Widget

logger = logging.getLogger(__name__)

# -- Defaults ---------------------------------------------------------------
DEFAULT_BG_ALPHA = 0.45          # How translucent the background box is
DEFAULT_TEXT_ALPHA = 0.95        # How opaque the text is
DEFAULT_PADDING = 12             # Padding inside the background box (pixels)
DEFAULT_CORNER_RADIUS = 10       # Rounded corner radius (pixels)


class ClockWidget(Widget):
    """Displays the current time as a digital clock with a translucent overlay.

    The clock is rendered over all other content (high z_index) with a
    semi-transparent dark background for readability against any image.

    Config options (via ``settings`` dict or constructor kwargs):
        - ``style``: ``"digital"`` (default) or ``"analog"`` (fallback to digital)
        - ``show_seconds``: Show ``HH:MM:SS`` instead of ``HH:MM`` (default False)
        - ``hour_format``: ``"24h"`` (default) or ``"12h"``
        - ``background_alpha``: Opacity of the background box (default 0.35)
        - ``text_alpha``: Opacity of the time text (default 0.95)
        - ``font_size``: Override automatic font sizing from widget height
        - ``padding``: Pixels between text and background edge (default 12)
    """

    def __init__(
        self,
        position: tuple[int, int] = (50, 50),
        size: tuple[int, int] = (260, 90),
        **kwargs: Any,
    ) -> None:
        # High z_index so the clock draws over the slideshow and other widgets
        super().__init__(
            "clock",
            position=position,
            size=size,
            z_index=50,
            refresh_interval=1,
            settings=kwargs,
        )
        self._style: str = self.settings.get("style", "digital")
        self._show_seconds: bool = self.settings.get("show_seconds", False)
        self._hour_format: str = self.settings.get("hour_format", "24h")
        self._background_alpha: float = float(
            self.settings.get("background_alpha", DEFAULT_BG_ALPHA)
        )
        self._text_alpha: float = float(
            self.settings.get("text_alpha", DEFAULT_TEXT_ALPHA)
        )
        self._font_size_override: int = self.settings.get("font_size", 0)
        self._padding: int = int(self.settings.get("padding", DEFAULT_PADDING))
        self._current_time: str = ""
        self._current_date: str = ""

    # -- Data update ---------------------------------------------------------

    def update(self, shared_state: dict[str, Any]) -> None:
        """Update the current time and date strings."""
        now = time.localtime()

        # Time format
        if self._hour_format == "12h":
            fmt = "%I:%M" if not self._show_seconds else "%I:%M:%S"
            time_str = time.strftime(fmt, now)
            # Strip leading zero from hour in 12h mode for cleaner look
            if time_str[0] == "0":
                time_str = " " + time_str[1:]
        else:
            fmt = "%H:%M" if not self._show_seconds else "%H:%M:%S"
            time_str = time.strftime(fmt, now)

        # AM/PM suffix for 12h mode
        if self._hour_format == "12h":
            ampm = "AM" if now.tm_hour < 12 else "PM"
            self._current_time = f"{time_str} {ampm}"
        else:
            self._current_time = time_str

        # Date (e.g. "Friday, 18 July")
        self._current_date = time.strftime("%A, %d %B", now)

    # -- Rendering -----------------------------------------------------------

    def draw(self, backend: DisplayBackend) -> None:
        """Render the clock with a translucent background box."""
        if not self._visible:
            return

        x, y = self.position
        w, h = self.size
        font_size = self._font_size_override or max(h // 3, 18)
        date_font_size = max(font_size // 3, 10)

        # 1. Draw translucent background
        self._draw_background(backend, x, y, w, h)

        # 2. Draw time text (centered horizontally in the widget box)
        if self._style in ("digital", "analog"):
            self._draw_digital(backend, x, y, w, h, font_size, date_font_size)

    def _draw_background(
        self,
        backend: DisplayBackend,
        x: float,
        y: float,
        w: float,
        h: float,
    ) -> None:
        """Draw a rounded semi-transparent dark background.

        Uses two overlapping rectangles to simulate rounded corners:
        - A full-size filled rect with the background colour
        - Does NOT use rounded corners natively (display backend doesn't
          support them).  The visual effect is still clean and unobtrusive.
        """
        bg_color = (0.08, 0.08, 0.12, self._background_alpha)
        # Draw just behind the text
        backend.draw_rect(x, y, w, h, bg_color, z=self.z_index - 1)

    def _draw_digital(
        self,
        backend: DisplayBackend,
        x: float,
        y: float,
        w: float,
        h: float,
        font_size: int,
        date_font_size: int,
    ) -> None:
        """Render the digital time and date text."""
        # Estimate text dimensions for centering
        # ~0.55 * font_size per character width (rough heuristic for monospace-ish)
        char_width = font_size * 0.58
        time_chars = len(self._current_time)
        text_width = char_width * time_chars

        # Center the time horizontally in the widget box
        time_x = x + (w - text_width) / 2
        # Position vertically: top half for time, bottom for date
        time_y = y + h * 0.25

        backend.draw_text(
            self._current_time,
            int(time_x),
            int(time_y),
            font_size=font_size,
            color=(1.0, 1.0, 1.0, self._text_alpha),
            z=self.z_index,
        )

        # Date below the time
        if self._current_date:
            date_width = len(self._current_date) * date_font_size * 0.55
            date_x = x + (w - date_width) / 2
            date_y = y + h * 0.65
            backend.draw_text(
                self._current_date,
                int(date_x),
                int(date_y),
                font_size=date_font_size,
                color=(0.75, 0.75, 0.80, self._text_alpha * 0.85),
                z=self.z_index,
            )
