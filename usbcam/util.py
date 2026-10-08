#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用小工具：时间戳、数值裁剪、ASCII 过滤、字体查找。"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# 1ms = 10 个 V4L2 曝光单位（exposure_time_absolute 的单位是 100us）
RAW_PER_MS = 10.0

# 窗口名一律 ASCII：中文窗口名在部分 OpenCV/X11 环境下直接打不开
WINDOW_MAIN = "USB Camera Monitor"
WINDOW_QR = "QR Result"


def now_stamp() -> str:
    """本地时间戳（毫秒 + 时区），如 2026-10-08 19:20:27.123 +08:00"""
    dt = datetime.now().astimezone()
    off = dt.strftime("%z")
    off = f"{off[:3]}:{off[3:]}" if len(off) == 5 else off
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d} " + off


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def ascii_safe(text: str, limit: int = 40) -> str:
    """只保留可打印 ASCII —— OpenCV Hershey 字体渲染不了中文，窗口文字必须纯 ASCII。"""
    if not text:
        return ""
    return "".join(ch for ch in text if 32 <= ord(ch) < 127)[:limit]


def has_cjk(text: str) -> bool:
    return any(ord(ch) > 0x2E7F for ch in (text or ""))


_CJK_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
)


def find_cjk_font(explicit: Optional[str] = None) -> Optional[str]:
    """找一个能渲染中文的字体文件；找不到返回 None（调用方退回 ASCII）。"""
    if explicit:
        return explicit if Path(explicit).exists() else None
    env = os.environ.get("USBCAM_FONT")
    if env and Path(env).exists():
        return env
    for p in _CJK_FONT_CANDIDATES:
        if Path(p).exists():
            return p
    # 兜底：在常见字体目录里找带 CJK/Noto 字样的字体
    for root in ("/usr/share/fonts", str(Path.home() / ".fonts"),
                 str(Path.home() / ".local/share/fonts")):
        base = Path(root)
        if not base.exists():
            continue
        for pat in ("**/*CJK*.tt[cf]", "**/*wqy*.tt[cf]", "**/*Noto*SC*.otf"):
            hits = sorted(base.glob(pat))
            if hits:
                return str(hits[0])
    return None


def wrap_text(text: str, width: int) -> List[str]:
    """按显示宽度折行（CJK 视作 2 个字符宽）。"""
    lines: List[str] = []
    cur = ""
    cur_w = 0
    for ch in text:
        w = 2 if ord(ch) > 0x2E7F else 1
        if ch == "\n" or cur_w + w > width:
            lines.append(cur)
            cur, cur_w = "", 0
            if ch == "\n":
                continue
        cur += ch
        cur_w += w
    if cur:
        lines.append(cur)
    return lines
