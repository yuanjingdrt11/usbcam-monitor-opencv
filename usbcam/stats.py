#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""帧统计：平均亮度、高光溢出比例、死白比例、分位数。

只统计 ROI（调用方传入的就已经是 ROI 图），为省算力先降采样到短边 320。
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class FrameStats:
    luma: float = 0.0        # 平均亮度 0..255
    clip_pct: float = 0.0    # >= clip_threshold 的像素占比(%)
    sat_pct: float = 0.0     # == 255 的死白占比(%)
    p999: float = 0.0        # 99.9 分位亮度


def analyse(bgr: np.ndarray, clip_threshold: int = 250, small_side: int = 320) -> FrameStats:
    h, w = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if min(h, w) > small_side:
        scale = small_side / float(min(h, w))
        gray = cv2.resize(gray, (max(1, int(w * scale)), max(1, int(h * scale))),
                          interpolation=cv2.INTER_AREA)
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel()
    total = float(gray.size) or 1.0
    cdf = np.cumsum(hist) / total
    idx999 = int(np.searchsorted(cdf, 0.999))
    return FrameStats(luma=float(gray.mean()),
                      clip_pct=float(hist[clip_threshold:].sum() / total * 100.0),
                      sat_pct=float(hist[255] / total * 100.0),
                      p999=float(min(idx999, 255)))
