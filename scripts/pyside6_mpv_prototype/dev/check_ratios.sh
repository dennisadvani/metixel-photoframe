#!/bin/bash
cd /opt/metixel/data/media/sample_media
echo "=== images ==="
for f in *.jpg; do
  python3 -c "from PIL import Image; im=Image.open('$f'); w,h=im.size; print(f'$f {w}x{h} ratio={w/h:.3f}')"
done
echo "=== video ==="
ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 13131508_1920_1080_24fps.mp4