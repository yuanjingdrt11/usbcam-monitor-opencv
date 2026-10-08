#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ROI 切割。

ROI 在这里承担三件事：
  1. 只处理感兴趣区域 -> 每帧计算量按面积下降，处理/显示帧率与延迟直接改善；
  2. 相机若支持 UVC ROI(V4L2 selection) 就同时下发硬件裁剪，让传感器少出像素；
  3. 曝光统计、高光压缩、扫码都只针对该区域，避免背景过曝/背景二维码干扰。

注意：采集帧率的上限由相机模式决定（例如某些相机 YUYV 1280x720 只有 10fps，
640x480 才有 30fps），ROI 不会凭空超过该上限；程序会把"采集帧率"和
"处理帧率"分开显示，实测数据说话。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
import numpy as np

from . import config, v4l2


@dataclass
class Roi:
    """像素坐标的兴趣区域。"""
    x: int
    y: int
    w: int
    h: int

    # --- 构造 ---
    @staticmethod
    def full(width: int, height: int) -> "Roi":
        return Roi(0, 0, max(1, width), max(1, height))

    @staticmethod
    def parse(spec: str, frame_w: int, frame_h: int) -> Optional["Roi"]:
        """解析 'x,y,w,h'（像素）或 'x,y,w,h%'（相对，0~100）或 'x,y,w,h!'（0~1 小数）。"""
        if not spec:
            return None
        s = spec.strip()
        rel = s.endswith("%") or s.endswith("!")
        unit = s[-1] if s and s[-1] in "%!" else ""
        if unit:
            s = s[:-1]
        try:
            parts = [float(v) for v in s.replace("x", ",").replace(":", ",").split(",") if v != ""]
        except ValueError:
            return None
        if len(parts) != 4:
            return None
        if unit == "%":
            parts = [p / 100.0 for p in parts]
        elif unit == "!":
            pass
        else:
            rel = False
        if rel:
            parts = [parts[0] * frame_w, parts[1] * frame_h,
                     parts[2] * frame_w, parts[3] * frame_h]
        return Roi(int(round(parts[0])), int(round(parts[1])),
                   int(round(parts[2])), int(round(parts[3]))).clamp(frame_w, frame_h)

    # --- 变换 ---
    def clamp(self, frame_w: int, frame_h: int, min_side: int = 16) -> "Roi":
        w = max(min_side, min(int(self.w), frame_w))
        h = max(min_side, min(int(self.h), frame_h))
        x = max(0, min(int(self.x), frame_w - w))
        y = max(0, min(int(self.y), frame_h - h))
        return Roi(x, y, w, h)

    def apply(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        if self.is_full(w, h):
            return frame
        r = self.clamp(w, h)
        return frame[r.y:r.y + r.h, r.x:r.x + r.w]

    def is_full(self, frame_w: int, frame_h: int) -> bool:
        return self.x <= 0 and self.y <= 0 and self.w >= frame_w and self.h >= frame_h

    def rect(self) -> Tuple[int, int, int, int]:
        return self.x, self.y, self.w, self.h

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @staticmethod
    def from_dict(d: Optional[dict]) -> Optional["Roi"]:
        if not d:
            return None
        try:
            return Roi(int(d["x"]), int(d["y"]), int(d["w"]), int(d["h"]))
        except Exception:
            return None

    def label(self) -> str:
        return f"{self.w}x{self.h}+{self.x}+{self.y}"


# ---------------------------------------------------------------------------
# 记忆
# ---------------------------------------------------------------------------
def save(roi: Optional[Roi], frame_w: int = 0, frame_h: int = 0) -> None:
    """把 ROI 以"相对比例"存起来，换分辨率也能沿用。"""
    if roi is None or frame_w <= 0 or frame_h <= 0:
        config.update(roi=None)
        return
    config.update(roi={"x": roi.x / frame_w, "y": roi.y / frame_h,
                       "w": roi.w / frame_w, "h": roi.h / frame_h})


def load(frame_w: int, frame_h: int) -> Optional[Roi]:
    d = config.get("roi")
    if not isinstance(d, dict):
        return None
    try:
        return Roi(int(round(float(d["x"]) * frame_w)), int(round(float(d["y"]) * frame_h)),
                   int(round(float(d["w"]) * frame_w)), int(round(float(d["h"]) * frame_h))
                   ).clamp(frame_w, frame_h)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 硬件裁剪（UVC ROI）
# ---------------------------------------------------------------------------
def try_hardware_crop(node: str, roi: Roi, frame_w: int, frame_h: int,
                      enable: bool = True) -> str:
    """尝试把 ROI 下发给相机做硬件裁剪，返回一句人话说明结果。"""
    if not enable:
        return "硬件裁剪: 已禁用（--no-hw-crop）"
    if roi.is_full(frame_w, frame_h):
        return "硬件裁剪: ROI 为整幅，无需硬件裁剪"
    bounds = v4l2.probe_crop(node)
    if bounds is None:
        return "硬件裁剪: 该相机/驱动不支持 V4L2 selection，使用软件 ROI"
    if v4l2.set_crop(node, roi.x, roi.y, roi.w, roi.h):
        return (f"硬件裁剪: 已下发 {roi.label()}（相机 crop_bounds 上限 "
                f"{bounds[0]}x{bounds[1]}）")
    return "硬件裁剪: 相机拒绝了裁剪请求（常见于 UVC 相机），使用软件 ROI"


# ---------------------------------------------------------------------------
# 鼠标框选
# ---------------------------------------------------------------------------
class RoiSelector:
    """交互式框选：进入选择态后按住左键拖拽，松手生效，Esc 取消。"""

    def __init__(self) -> None:
        self.active = False
        self.dragging = False
        self.p0: Tuple[int, int] = (0, 0)
        self.p1: Tuple[int, int] = (0, 0)
        self.done: Optional[Roi] = None

    # --- 事件 ---
    def on_mouse(self, event: int, x: int, y: int, flags: int, param) -> None:
        if not self.active:
            return
        if event == cv2.EVENT_LBUTTONDOWN:
            self.dragging = True
            self.p0 = self.p1 = (x, y)
        elif event == cv2.EVENT_MOUSEMOVE and self.dragging:
            self.p1 = (x, y)
        elif event == cv2.EVENT_LBUTTONUP and self.dragging:
            self.dragging = False
            self.p1 = (x, y)
            x0, x1 = sorted((self.p0[0], self.p1[0]))
            y0, y1 = sorted((self.p0[1], self.p1[1]))
            if x1 - x0 >= 16 and y1 - y0 >= 16:
                self.done = Roi(x0, y0, x1 - x0, y1 - y0)
            self.active = False

    def cancel(self) -> None:
        self.active = False
        self.dragging = False
        self.done = None

    def take(self) -> Optional[Roi]:
        r, self.done = self.done, None
        return r

    # --- 绘制 ---
    def draw(self, img: np.ndarray) -> None:
        if not self.active:
            return
        h, w = img.shape[:2]
        cv2.putText(img, "DRAG TO SELECT ROI  (Esc=cancel)", (12, h - 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(img, "DRAG TO SELECT ROI  (Esc=cancel)", (12, h - 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 1, cv2.LINE_AA)
        if self.dragging:
            cv2.rectangle(img, self.p0, self.p1, (0, 255, 255), 2)


def draw_roi_box(img: np.ndarray, roi: Roi) -> None:
    """在全幅视图上标出 ROI。"""
    x, y, w, h = roi.rect()
    cv2.rectangle(img, (x, y), (x + w, y + h), (0, 255, 255), 1)
