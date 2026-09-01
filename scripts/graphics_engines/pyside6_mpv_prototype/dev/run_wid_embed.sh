#!/bin/bash
# Run the wid-embedding test under cage
cd /tmp/metixel-pyside-mpv
timeout 20 cage -- python3 test_wid_embed.py 2>&1 | tail -30