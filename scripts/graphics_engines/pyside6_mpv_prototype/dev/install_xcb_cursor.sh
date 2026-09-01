#!/bin/bash
# Install libxcb-cursor0 for Qt xcb platform plugin
sudo apt-get install -y libxcb-cursor0 2>&1 | tail -5
echo "=== verify ==="
ldconfig -p 2>/dev/null | grep xcb-cursor || find / -name 'libxcb-cursor*' 2>/dev/null