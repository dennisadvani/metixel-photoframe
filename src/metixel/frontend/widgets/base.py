# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Widget base class — abstract overlay widget interface.

Widgets render on a transparent overlay layer above the slideshow.
Each widget implements :meth:`update` (fetch/refresh data) and
:meth:`draw` (render via the display backend primitives).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from metixel.display.backend import DisplayBackend


class Widget(ABC):
    """Abstract base for overlay widgets (clock, weather, etc.).

    Subclasses implement :meth:`update` and :meth:`draw` and manage
    their own rendering using backend primitives (``draw_text``,
    ``draw_rect``, ``draw_image``).
    """

    def __init__(
        self,
        name: str,
        position: tuple[int, int] = (0, 0),
        size: tuple[int, int] = (0, 0),
        *,
        z_index: int = 0,
        refresh_interval: int = 0,
        settings: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._name = name
        self._position = position
        self._size = size
        self._z_index = z_index
        self._refresh_interval = refresh_interval
        self._visible = True
        self._settings: dict[str, Any] = dict(settings or {})
        if kwargs:
            self._settings.update(kwargs)

    # -- Properties ----------------------------------------------------------

    @property
    def name(self) -> str:
        """Unique widget name (for logging/identification)."""
        return self._name

    @property
    def position(self) -> tuple[int, int]:
        """Top-left position on screen."""
        return self._position

    @property
    def size(self) -> tuple[int, int]:
        """Widget dimensions."""
        return self._size

    @property
    def z_index(self) -> int:
        """Draw order (higher = on top)."""
        return self._z_index

    @property
    def refresh_interval(self) -> int:
        """Seconds between :meth:`update` calls."""
        return self._refresh_interval

    @property
    def visible(self) -> bool:
        """Show/hide toggle."""
        return self._visible

    @visible.setter
    def visible(self, value: bool) -> None:
        self._visible = value

    @property
    def settings(self) -> dict[str, Any]:
        """Widget-specific configuration."""
        return self._settings

    # -- Interface -----------------------------------------------------------

    @abstractmethod
    def update(self, shared_state: dict[str, Any]) -> None:
        """Fetch/recompute widget data (called every refresh interval)."""

    @abstractmethod
    def draw(self, backend: DisplayBackend) -> None:
        """Render the widget using backend primitives."""

    def needs_refresh(self) -> bool:
        """Whether the widget should refresh on this tick."""
        return True
