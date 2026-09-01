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

## Screen PIN (future on-screen UI)

The optional **screen PIN** protects the future on-screen UI on the frame. It is
a **backend contract** already implemented; the on-screen keypad widget is
future work (planned for the PySide6 migration).

### Backend contract

- **Storage:** `web.screen_pin` in `config.json`, stored as a salted hash
  (`metixel.shared.security.hash_secret`). Empty string = disabled.
- **Service:** `metixel.backend.web.auth.ScreenPinService` — validates a
  candidate PIN with a **constant-time compare** (`hmac.compare_digest`),
  enforces an attempt limit (`MAX_PIN_ATTEMPTS = 3`) + cooldown
  (`PIN_COOLDOWN_SECONDS = 600`), and tracks an unlock window of
  `web.screen_pin_timeout_minutes` (capped at 1440 / 24 h).
- **API:** `POST /api/auth/screen-pin` (set/change/clear, requires an
  authenticated web session) and `GET /api/auth/screen-pin/status`.
- **PIN format:** 4–6 digits, stored/compared as **strings** (never ints) so
  leading zeros are preserved.

### Integration hook for the future keypad widget

When the on-screen UI is built, the keypad widget should:

1. Render a numeric grid (0–9, backspace, OK) fully operable with **directional
   keys** (up/down/left/right to move focus, OK/Enter to press, back to delete).
2. On submit, call `ScreenPinService.validate(candidate)` (or the equivalent
   IPC/HTTP path) and check `is_unlocked()` before showing the menu.
3. On `MAX_PIN_ATTEMPTS` failures, show the cooldown message returned by
   `validate()` and disable input until it elapses.

The PIN is **independent** of the web dashboard password and the device
password — entering it on the frame does not unlock the web dashboard, and vice
versa. It is also distinct from the AP-fallback PIN in `network_controller.py`
(which is a 4-digit code shown on-screen to join Wi-Fi via the captive portal).
