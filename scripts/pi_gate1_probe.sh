#!/bin/bash
# GATE-1 prerequisite: identify the sample videos' codecs.
# The HEVC comparison is meaningless unless we know what is being decoded.

for f in \
    /opt/metixel/data/media/sample_media/landscape/13131508_1920_1080_24fps.mp4 \
    /opt/metixel/data/media/sample_media/portrait/15616361_1080_1920_30fps.mp4
do
    echo "=== $(basename "$f") ==="
    ffprobe -v error \
        -show_entries stream=codec_name,profile,width,height,pix_fmt,level \
        -show_entries format=duration,bit_rate \
        -of default=noprint_wrappers=1 "$f" 2>&1 | sed 's/^/  /'
    echo
done
