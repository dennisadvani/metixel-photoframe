#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Send a 'hide' trigger to the cursor-hider service.

Used by metixel-cage.service's ExecStartPre to park the cursor off-screen
BEFORE cage starts.  Best-effort — exits 0 even if the service isn't running.
"""

import json
import socket
import sys

SOCKET_PATH = "/run/metixel/cursor-hider.sock"


def main() -> int:
    if not hasattr(socket, "AF_UNIX"):
        return 0
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.sendto(json.dumps({"cmd": "hide"}).encode("utf-8"), SOCKET_PATH)
        finally:
            s.close()
    except Exception:  # noqa: BLE001 - best-effort
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
