#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v4l2-ctl 封装：控件读写、模式枚举、硬件裁剪尝试。

OpenCV 5.0 的 Python 绑定没有 CAP_PROP_POWER_LINE_FREQUENCY，也不能写任意
V4L2 CID，所以这些项走 v4l2-ctl；没有该命令时全部优雅降级（返回 None/False）。
本模块不含任何本机特有常量，CID 均为 V4L2 规范值。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# V4L2 规范中的控件 ID（不是本机特有值）
CID_GAIN = 0x00980913
CID_POWER_LINE = 0x00980918
CID_AUTO_EXPOSURE = 0x009A0901
CID_EXPOSURE_ABSOLUTE = 0x009A0902
CID_EXPOSURE_DYNAMIC_FRAMERATE = 0x009A0903


def have_v4l2ctl() -> bool:
    return shutil.which("v4l2-ctl") is not None


def run(args: List[str], timeout: float = 3.0) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 采集模式（分辨率 + 帧率 + 像素格式）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Mode:
    fourcc: str
    width: int
    height: int
    fps: float          # 设备声明的最大帧率

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def label(self) -> str:
        return f"{self.fourcc} {self.width}x{self.height}@{self.fps:g}"


_SIZE_RE = re.compile(r"Size:\s*Discrete\s+(\d+)x(\d+)")
_FPS_RE = re.compile(r"\((\d+(?:\.\d+)?)\s*fps\)")
_FMT_RE = re.compile(r"\[\d+\]:\s*'(\w+)'")


def parse_modes(text: str) -> List[Mode]:
    """解析 `v4l2-ctl --list-formats-ext` 的输出文本（抽出来便于单元测试）。"""
    modes: List[Mode] = []
    fourcc = ""
    size: Optional[Tuple[int, int]] = None
    best_fps: Optional[float] = None

    def flush() -> None:
        nonlocal size, best_fps
        if fourcc and size and best_fps:
            modes.append(Mode(fourcc, size[0], size[1], best_fps))
        size, best_fps = None, None

    for line in text.splitlines():
        m = _FMT_RE.search(line)
        if m:
            flush()
            fourcc = m.group(1)
            continue
        m = _SIZE_RE.search(line)
        if m:
            flush()
            size = (int(m.group(1)), int(m.group(2)))
            continue
        m = _FPS_RE.search(line)
        if m and size:
            v = float(m.group(1))
            best_fps = v if best_fps is None else max(best_fps, v)
    flush()
    return modes


def list_modes(node: str) -> List[Mode]:
    """查询设备支持的全部采集模式（分辨率 + 帧率 + 像素格式）。"""
    if not have_v4l2ctl():
        return []
    return parse_modes(run(["v4l2-ctl", "-d", node, "--list-formats-ext"]))


# ---------------------------------------------------------------------------
# 控件表
# ---------------------------------------------------------------------------
_CTRL_RE = re.compile(r"^\s*([a-z0-9_]+)\s+0x([0-9a-fA-F]{8})\s+\((\w+)\)\s*:\s*(.*)$")


class Controls:
    """读写 V4L2 控件（通过 v4l2-ctl）。"""

    def __init__(self, node: str, enabled: bool = True):
        self.node = node
        self.available = bool(enabled) and have_v4l2ctl()
        self.table: Dict[str, dict] = {}
        if self.available:
            self.refresh()

    def refresh(self) -> None:
        text = run(["v4l2-ctl", "-d", self.node, "--list-ctrls"])
        table: Dict[str, dict] = {}
        for line in text.splitlines():
            m = _CTRL_RE.match(line)
            if not m:
                continue
            name, cid, typ, rest = m.groups()
            info: dict = {"id": int(cid, 16), "type": typ}
            for k, v in re.findall(r"(\w+)=(\S+)", rest):
                info[k] = int(v, 0) if re.fullmatch(r"-?\d+|0x[0-9a-fA-F]+", v) else v
            table[name] = info
        self.table = table

    def has(self, name: str) -> bool:
        return name in self.table

    def info(self, name: str) -> dict:
        return self.table.get(name, {})

    def limit(self, name: str, key: str, default: int = 0) -> int:
        try:
            return int(self.info(name).get(key, default))
        except Exception:
            return default

    def get(self, name: str) -> Optional[int]:
        if not self.available or not self.has(name):
            return None
        text = run(["v4l2-ctl", "-d", self.node, "--get-ctrl", name])
        m = re.search(r":\s*(-?\d+)", text)
        if m:
            self.table[name]["value"] = int(m.group(1))
            return int(m.group(1))
        return None

    def set(self, name: str, value: int) -> bool:
        if not self.available or not self.has(name):
            return False
        run(["v4l2-ctl", "-d", self.node, "--set-ctrl", f"{name}={int(value)}"])
        return self.get(name) == int(value)


# ---------------------------------------------------------------------------
# 硬件裁剪（UVC ROI / V4L2 selection）—— 多数 UVC 相机不支持，试一下即可
# ---------------------------------------------------------------------------
def probe_crop(node: str) -> Optional[Tuple[int, int]]:
    """返回硬件裁剪的上限 (w,h)；不支持返回 None。"""
    if not have_v4l2ctl():
        return None
    text = run(["v4l2-ctl", "-d", node, "--all"])
    m = re.search(r"Selection Video Capture:\s*crop_bounds[^,]*,\s*Left \d+,\s*Top \d+,"
                  r"\s*Width (\d+),\s*Height (\d+)", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def set_crop(node: str, x: int, y: int, w: int, h: int) -> bool:
    """尝试设置硬件裁剪；成功返回 True（失败说明驱动/相机不支持 UVC ROI）。"""
    if not have_v4l2ctl():
        return False
    out = run(["v4l2-ctl", "-d", node, "--set-selection",
               f"target=crop,flags=,left={x},top={y},width={w},height={h}"])
    if "failed" in out.lower() or "invalid" in out.lower():
        return False
    got = run(["v4l2-ctl", "-d", node, "--get-selection=target=crop,which=active"])
    m = re.search(r"Left (\d+), Top (\d+), Width (\d+), Height (\d+)", got)
    return bool(m) and (int(m.group(3)), int(m.group(4))) == (w, h)
