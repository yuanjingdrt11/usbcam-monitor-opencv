#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""python3 -m usbcam 入口。"""

import sys

from .app import main

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:          # 任何命令下 Ctrl+C 都干净退出
        print("\n[退出] 已中断 (Ctrl+C)")
        sys.exit(130)
