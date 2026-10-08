#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图像增强：高光肩部压缩 (highlight roll-off)、CLAHE、溢出像素高亮。

肩部压缩作用在 LAB 的 L 通道上（纯亮度，不偏色），曲线性质：
    knee 以下逐值不变；knee 以上 g(u)=u-A*u*(1-u)，g(0)=0, g(1)=1，单调，
    且 g(u) <= u —— 只会压暗高光，绝不会把接近 255 的像素顶到 255；
    最后与恒等线取 min 并令 255->255，保证"只压不提亮"。

注意：已经到 255 的死白像素信息已丢失，任何曲线都无法恢复；真正解决过曝
靠设备侧手动曝光 + AEC，(这里) 只负责把濒临溢出的高光压回可用范围。
"""

from __future__ import annotations

from typing import Tuple

import cv2
import numpy as np

from .util import clamp


def build_tone_lut(knee: float = 0.78, strength: float = 0.6,
                   gamma: float = 1.0) -> np.ndarray:
    a = clamp(strength, 0.0, 1.0)
    k = clamp(knee, 0.05, 0.98)
    x = np.linspace(0.0, 1.0, 256, dtype=np.float64)

    def shoulder(v: np.ndarray) -> np.ndarray:
        out = v.copy()
        m = v > k
        if np.any(m) and a > 0:
            u = (v[m] - k) / (1.0 - k)
            out[m] = k + (1.0 - k) * (u - a * u * (1.0 - u))
        return out

    y = shoulder(x)
    kernel = np.array([1, 2, 3, 2, 1], dtype=np.float64)
    kernel /= kernel.sum()                      # 移动平均保持单调，只柔化 knee 折点
    y = np.convolve(np.pad(y, 2, mode="edge"), kernel, mode="valid")
    if abs(gamma - 1.0) > 1e-3:
        y = np.power(np.clip(y, 0.0, 1.0), gamma)
    lut = np.clip(y * 255.0 + 0.5, 0, 255).astype(np.uint8)
    lut = np.minimum(lut, np.arange(256, dtype=np.uint8))
    lut[0], lut[255] = 0, 255
    return lut


class Enhancer:
    """把 LUT + CLAHE 应用到 BGR 图（原地返回新图，不改输入）。"""

    def __init__(self, tone: bool = True, clahe: bool = False, knee: float = 0.78,
                 strength: float = 0.6, gamma: float = 1.0, clahe_clip: float = 2.0):
        self.tone = tone
        self.clahe_on = clahe
        self.knee = knee
        self.strength = strength
        self.gamma = gamma
        self._clahe = cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=(8, 8))
        self.lut = build_tone_lut(knee, strength, gamma)

    def rebuild(self) -> None:
        self.lut = build_tone_lut(self.knee, self.strength, self.gamma)

    def apply(self, bgr: np.ndarray) -> np.ndarray:
        if not (self.tone or self.clahe_on):
            return bgr
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        l = lab[:, :, 0]
        if self.tone:
            l = cv2.LUT(l, self.lut)
        if self.clahe_on:
            l = self._clahe.apply(l)
        lab[:, :, 0] = l
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def highlight_clipped(img: np.ndarray, clip_threshold: int = 250,
                      color: Tuple[int, int, int] = (255, 0, 255)) -> np.ndarray:
    """把接近/已经死白的像素染色，直观看到还在过曝的地方。"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = (gray >= clip_threshold).astype(np.uint8)
    if not mask.any():
        return img
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1).astype(bool)
    out = img.copy()
    tint = np.zeros_like(img)
    tint[:] = color
    out[mask] = (0.45 * img[mask] + 0.55 * tint[mask]).astype(np.uint8)
    return out


def side_by_side(raw: np.ndarray, proc: np.ndarray) -> np.ndarray:
    """原始 | 处理后 左右对比。"""
    h, w = raw.shape[:2]
    half = max(1, w // 2)
    left = cv2.resize(raw, (half, h), interpolation=cv2.INTER_AREA)
    right = cv2.resize(proc, (half, h), interpolation=cv2.INTER_AREA)
    out = np.hstack([left, right])
    cv2.line(out, (half, 0), (half, h), (0, 255, 255), 1)
    for text, x in (("RAW", 10), ("OPTIMIZED", half + 10)):
        cv2.putText(out, text, (x, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(out, text, (x, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 1, cv2.LINE_AA)
    return out
