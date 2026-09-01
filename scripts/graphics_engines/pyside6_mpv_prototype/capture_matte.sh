#!/bin/bash
# Capture screenshots of the first items to verify the virtual matte.
# The playlist is sorted: video first, then images. We capture item 0 (video,
# 16:9) and item 1 (image, 3:2) to see both letterbox and pillarbox.
cd /tmp/metixel-pyside-mpv
export QT_QPA_PLATFORM=wayland

echo "=== screenshot item 0 (video 16:9) ==="
timeout 15 cage -- python3 slideshow.py --media /opt/metixel/data/media/sample_media \
  --screenshot /tmp/metixel-pyside-mpv/shot_video.png 2>&1 | grep -iE 'screenshot|VO:|first video|error|bench' | head

echo "=== screenshot item 1 (image 3:2) ==="
# Use a media dir with only the 3:2 images to capture a clean matte shot
mkdir -p /tmp/metixel-pyside-mpv/matte32
cp "/opt/metixel/data/media/sample_media/pexels-brunorock-19674944 (Custom).jpg" /tmp/metixel-pyside-mpv/matte32/
timeout 15 cage -- python3 slideshow.py --media /tmp/metixel-pyside-mpv/matte32 \
  --screenshot /tmp/metixel-pyside-mpv/shot_32.png 2>&1 | grep -iE 'screenshot|VO:|error|bench' | head

echo "=== screenshot item 16:9 image ==="
mkdir -p /tmp/metixel-pyside-mpv/matte169
cp "/opt/metixel/data/media/sample_media/pexels-kienvirak-4962828 (Custom).jpg" /tmp/metixel-pyside-mpv/matte169/
timeout 15 cage -- python3 slideshow.py --media /tmp/metixel-pyside-mpv/matte169 \
  --screenshot /tmp/metixel-pyside-mpv/shot_169.png 2>&1 | grep -iE 'screenshot|VO:|error|bench' | head

echo "=== files ==="
ls -la /tmp/metixel-pyside-mpv/shot_*.png 2>/dev/null