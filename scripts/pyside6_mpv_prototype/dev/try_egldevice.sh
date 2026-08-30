#!/bin/bash
echo "=== deps of kms-egldevice plugin ==="
ldd /home/pi/.local/lib/python3.13/site-packages/PySide6/Qt/plugins/egldeviceintegrations/libqeglfs-kms-egldevice-integration.so 2>&1 | grep -iE 'not found|EglFs|Kms|Gbm'
echo "=== deps of kms-integration plugin ==="
ldd /home/pi/.local/lib/python3.13/site-packages/PySide6/Qt/plugins/egldeviceintegrations/libqeglfs-kms-integration.so 2>&1 | grep -iE 'not found|EglFs|Kms|Gbm'
echo "=== try eglfs_kms_egldevice integration ==="
cd /tmp/metixel-pyside-mpv && QT_QPA_PLATFORM=eglfs QT_QPA_EGLFS_INTEGRATION=eglfs_kms_egldevice QT_QPA_EGLFS_KMS_DEVS=/dev/dri/card1 timeout 15 python3 slideshow.py --media /opt/metixel/data/media/sample_media --duration 10 --out /tmp/metixel-pyside-mpv/benchmark.json 2>&1 | head -20