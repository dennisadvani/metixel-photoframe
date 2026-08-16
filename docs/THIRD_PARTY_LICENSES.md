# Third-Party Licenses

This document lists the third-party dependencies of Metixel Photoframe and their
respective open-source licenses. All dependencies are compatible with the
Apache License 2.0 under which Metixel Photoframe is distributed.

---

## Python Dependencies (Phase 1 — Raspberry Pi 2/3/Zero 2 W)

| Package | Version | License | Upstream |
|---|---|---|---|
| **pi3d** | ≥2.47 | MIT | https://github.com/tipam/pi3d |
| **Pillow** | ≥10.0 | HPND (Historical Permission Notice and Disclaimer) | https://github.com/python-pillow/Pillow |
| **numpy** | ≥1.24, <2.0 | BSD 3-Clause | https://github.com/numpy/numpy |
| **Flask** | ≥3.0 | BSD 3-Clause | https://github.com/pallets/flask |
| **watchdog** | ≥3.0 | Apache 2.0 | https://github.com/gorakhargosh/watchdog |
| **paho-mqtt** | ≥1.6 | EPL-2.0 / EDL-1.0 (dual) | https://github.com/eclipse/paho.mqtt.python |
| **requests** | ≥2.31 | Apache 2.0 | https://github.com/psf/requests |
| **pygame** | ≥2.5 | LGPL 2.1 | https://github.com/pygame/pygame |
| **pysdl2** | ≥0.9 | Public Domain / CC0 | https://github.com/py-sdl/py-sdl2 |
| **python-vlc** | ≥3.0 | LGPL 2.1+ | https://github.com/oaubert/python-vlc |
| **cec** | ≥0.2 | MIT | https://github.com/trainman419/python-cec |

## Python Dependencies (Phase 2 — Raspberry Pi 4/5, Radxa Zero 3W)

| Package | Version | License | Upstream |
|---|---|---|---|
| **PyOpenGL** | ≥3.1 | BSD 3-Clause | https://github.com/mcfletch/pyopengl |

## Python Dependencies (Development)

| Package | Version | License | Upstream |
|---|---|---|---|
| **ruff** | ≥0.3 | MIT | https://github.com/astral-sh/ruff |
| **mypy** | ≥1.8 | MIT | https://github.com/python/mypy |
| **pytest** | ≥8.0 | MIT | https://github.com/pytest-dev/pytest |
| **pytest-cov** | ≥4.1 | MIT | https://github.com/pytest-dev/pytest-cov |

## System Dependencies (Trixie / Debian 13)

| Package | License | Purpose |
|---|---|---|
| **cage** | MIT | Minimal Wayland compositor (kiosk mode) |
| **xwayland** | MIT | X11 compatibility layer for pi3d |
| **Mesa** | MIT | OpenGL / EGL implementation |
| **Linux kernel (vc4/v3d DRM)** | GPL-2.0 | GPU kernel drivers |

## License Compatibility Notes

- **paho-mqtt** (EPL-2.0) is compatible with Apache 2.0. The Eclipse Public
  License 2.0 includes a secondary compatibility clause with the GPL family,
  and is fully compatible with permissive licenses including Apache 2.0.
- **pygame** (LGPL 2.1) and **python-vlc** (LGPL 2.1+) are dynamically linked
  at runtime. LGPL permits this usage without imposing copyleft requirements
  on the calling application.
- All other dependencies use permissive licenses (MIT, BSD, Apache 2.0, HPND)
  that are fully compatible with Apache 2.0.

---

*This file is maintained by the Metixel Photoframe Contributors. If you notice an
error or omission, please open an issue or pull request.*
