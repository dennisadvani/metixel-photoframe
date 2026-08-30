#!/usr/bin/env python3
from PIL import Image
import sys
im = Image.open(sys.argv[1])
print("size:", im.size, "mode:", im.mode)
