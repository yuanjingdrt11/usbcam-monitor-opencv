#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容入口：保留原来的文件名，内部调用模块化的 usbcam 包。

    python3 usb_cam_overexposure.py [参数]     等价于 python3 -m usbcam [参数]

窗口名固定 ASCII（"USB Camera Monitor" / "QR Result"）——中文窗口名
在部分 OpenCV/X11 环境下会直接导致窗口打不开。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from usbcam.app import main   # noqa: E402

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[退出] 已中断 (Ctrl+C)")
        sys.exit(130)
