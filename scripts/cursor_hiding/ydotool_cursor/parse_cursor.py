#!/usr/bin/env python3
"""Parse an XCursor file and print image info + first pixel."""
import struct
import sys

path = sys.argv[1]
d = open(path, "rb").read()
magic, header, version, ntoc = struct.unpack("IIII", d[:16])
print(f"magic={magic:#x} header={header} version={version} ntoc={ntoc}")
off = 16
for i in range(ntoc):
    ctype, subtype, pos = struct.unpack("III", d[off : off + 12])
    off += 12
    print(f"toc: type={ctype:#x} subtype(size)={subtype} pos={pos}")
    ctype2, chead, cver = struct.unpack("III", d[pos : pos + 12])
    w, h, xh, yh, delay = struct.unpack("IIIII", d[pos + 12 : pos + 32])
    print(f"  image: {w}x{h} hot=({xh},{yh}) delay={delay}")
    px = struct.unpack("I", d[pos + 32 : pos + 36])[0]
    print(f"  pixel=0x{px:08x} alpha={(px >> 24) & 0xff}")
