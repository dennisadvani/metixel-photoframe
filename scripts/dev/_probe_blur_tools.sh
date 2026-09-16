#!/bin/bash
# What blur primitives are available in the Pi's environment?
python3 - <<'PY'
checks = [
    ("PySide6", "import PySide6; print('  PySide6', PySide6.__version__)"),
    ("QGraphicsBlurEffect", "from PySide6.QtWidgets import QGraphicsBlurEffect; print('  QGraphicsBlurEffect OK')"),
    ("QImage scaled", "from PySide6.QtGui import QImage; print('  QImage.scaled OK')"),
    ("PIL ImageFilter", "from PIL import ImageFilter; print('  PIL ImageFilter OK')"),
    ("PIL GaussianBlur", "from PIL import ImageFilter; print('  GaussianBlur OK')"),
    ("PIL BoxBlur", "from PIL import ImageFilter; print('  BoxBlur OK')"),
    ("numpy", "import numpy; print('  numpy', numpy.__version__)"),
]
for name, code in checks:
    try:
        exec(code)
    except Exception as exc:
        print(f"  {name}: MISSING ({exc.__class__.__name__})")
PY

echo
echo "=== is the frontend's PIL import path usable? (Pillow is a runtime dep) ==="
PYTHONPATH=/opt/metixel/live/src python3 -c "
from PIL import ImageFilter
print('  Pillow reachable from the app env')
"
