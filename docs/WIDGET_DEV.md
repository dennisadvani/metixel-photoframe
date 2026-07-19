# Metixel Photoframe Widget Development Guide

Widgets are Python classes that render overlay content on top of the slideshow.

## Quick Start

```python
from metixel.frontend.widgets.base import Widget
from metixel.display.backend import DisplayBackend

class HelloWorldWidget(Widget):
    def __init__(self, position=(100, 100), **kwargs):
        super().__init__("hello_world", position, (200, 50), z_index=10, refresh_interval=5, **kwargs)

    def update(self, shared_state):
        # Fetch data (called every refresh_interval seconds)
        pass

    def draw(self, backend: DisplayBackend):
        if not self.visible:
            return
        backend.draw_text("Hello, World!", *self.position, font_size=24, color=(1,1,1,1))
```

## Widget Interface

### Required Methods
- `update(shared_state: dict)` — Fetch/recompute widget data
- `draw(backend: DisplayBackend)` — Render the widget

### Properties
- `position: tuple[int, int]` — Top-left position on screen
- `size: tuple[int, int]` — Widget dimensions
- `z_index: int` — Draw order (higher = on top)
- `refresh_interval: int` — Seconds between data updates
- `visible: bool` — Show/hide toggle
- `settings: dict` — Widget-specific configuration

## Best Practices
- Keep `draw()` fast — avoid network calls
- Use `needs_refresh()` to batch data fetches
- Respect `visible` property
- Use backend primitives (`draw_text`, `draw_rect`, `draw_image`)
- Never import hardware-specific libraries directly
