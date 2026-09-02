#!/usr/bin/env python3
"""Send a 'hide' trigger to the cursor-hider service."""
import json
import socket
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "/run/metixel/cursor-hider.sock"
s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
s.sendto(json.dumps({"cmd": "hide"}).encode("utf-8"), path)
s.close()
print("sent hide trigger to", path)