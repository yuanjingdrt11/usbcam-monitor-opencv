#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""窗口叠加层：右上角核心参数面板、左上角快捷键面板。

窗口内所有文字必须 ASCII —— OpenCV 的 Hershey 字体无法渲染中文
（中文会显示成方框/乱码），因此这里统一走 util.ascii_safe()。
窗口名同样必须 ASCII，定义在 util.WINDOW_MAIN / util.WINDOW_QR。
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

from . import util

FONT = cv2.FONT_HERSHEY_SIMPLEX
Color = Tuple[int, int, int]

C_TITLE: Color = (60, 220, 255)
C_OK: Color = (140, 255, 140)
C_WARN: Color = (80, 80, 255)
C_DIM: Color = (200, 200, 200)
C_ID: Color = (255, 220, 140)
C_WHITE: Color = (255, 255, 255)
C_CLIP_BAD: Color = (60, 60, 255)
C_QR: Color = (120, 255, 120)

HELP_LINES = (
    "q/Esc quit       a AEC on/off",
    "t tone on/off    c CLAHE on/off",
    "b compare raw|proc",
    "k clipped-pixel highlight",
    "e camera auto-exposure",
    "i show SN / install ID",
    "z select ROI (drag)  o ROI on/off",
    "n next camera",
    "+/- exposure +-1ms",
    "g gamma cycle    r reset AEC",
    "s snapshot       p hold AEC",
    "y rescan QR      h hide help",
)


def text_size(text: str, scale: float, thick: int = 1) -> Tuple[int, int]:
    (w, h), _ = cv2.getTextSize(text, FONT, scale, thick)
    return w, h


def panel(img: np.ndarray, lines: Sequence[Tuple[str, Color]], align: str = "right",
          scale: float = 0.52, margin: int = 12, pad: int = 8,
          title: Optional[str] = None) -> None:
    """在 img 上画半透明底板 + 多行文字（右上/左上），纯 ASCII。"""
    if not lines:
        return
    sizes = [text_size(t, scale) for t, _ in lines]
    line_h = max(s[1] for s in sizes) + 7
    box_w = max(s[0] for s in sizes) + pad * 2
    if title:
        box_w = max(box_w, text_size(title, scale)[0] + pad * 2)
    box_h = line_h * len(lines) + pad * 2 + (line_h if title else 0)
    h, w = img.shape[:2]
    x2 = w - margin if align == "right" else margin + box_w
    x1, y1 = max(0, x2 - box_w), margin
    x2, y2 = min(w, x2), min(h, y1 + box_h)
    roi = img[y1:y2, x1:x2]
    if roi.size:
        dark = np.zeros_like(roi)
        dark[:] = (12, 12, 12)
        cv2.addWeighted(dark, 0.58, roi, 0.42, 0, roi)

    y = y1 + pad + line_h - 6
    if title:
        cv2.putText(img, title, (x1 + pad, y), FONT, scale, C_TITLE, 1, cv2.LINE_AA)
        y += line_h
    for (text, color), (tw, _) in zip(lines, sizes):
        x = (x2 - pad - tw) if align == "right" else (x1 + pad)
        cv2.putText(img, text, (x, y), FONT, scale, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, text, (x, y), FONT, scale, color, 1, cv2.LINE_AA)
        y += line_h


def help_panel(img: np.ndarray, scale: float = 0.5) -> None:
    panel(img, [(t, (200, 230, 255)) for t in HELP_LINES], align="left", scale=scale, title="KEYS")


def banner(img: np.ndarray, text: str, color: Color = C_WARN, scale: float = 0.6) -> None:
    """左上角一条通栏提示（如 PAUSED / 切换相机中）。"""
    tw, th = text_size(text, scale)
    cv2.rectangle(img, (0, 0), (min(img.shape[1], tw + 20), th + 16), (0, 0, 0), -1)
    cv2.putText(img, text, (10, th + 6), FONT, scale, color, 1, cv2.LINE_AA)


def hud_lines(meta, cam, fps_cap: float, fps_proc: float, img: np.ndarray, exp: float,
              st, ctrl, enh, aec_on: bool, auto_exp: bool, roi_label: str,
              scanner, paused: bool, show_ids: bool,
              dropped: int = 0, at_ceiling: bool = False) -> List[Tuple[str, Color]]:
    """右上角核心参数面板的内容（纯 ASCII，中文会被 util.ascii_safe 过滤）。"""
    h, w = img.shape[:2]
    clip_color = C_CLIP_BAD if st.clip_pct > 1.2 else C_OK
    lines: List[Tuple[str, Color]] = [
        (f"RES   {cam.resolution()[0]}x{cam.resolution()[1]} {cam.fourcc()}", C_OK),
        (f"FPS   cam {fps_cap:5.1f}{' MAX' if at_ceiling else '    '} / proc {fps_proc:5.1f}"
         + (f"  drop {dropped}" if dropped else ""), C_OK if at_ceiling else C_DIM),
        (f"ROI   {roi_label}  ({w}x{h})", C_DIM),
        (f"EXP   {exp / util.RAW_PER_MS:5.2f} ms  raw {exp:4.0f}"
         f"  [{'AUTO-DEV' if auto_exp else 'MANUAL'}]", (120, 220, 255)),
        (f"GAIN  {'n/a' if cam.gain() is None else cam.gain()}", C_DIM),
        (f"CLIP  {st.clip_pct:5.2f}%   dead-white {st.sat_pct:4.2f}%", clip_color),
        (f"LUMA  {st.luma:5.1f}", C_DIM),
        (f"AEC   {'ON ' if (aec_on and not paused) else 'OFF'} {ctrl.reason_en[:34]}",
         C_DIM if aec_on else C_WARN),
        (f"TONE  {'ON ' if enh.tone else 'OFF'} knee {enh.knee:.2f} x{enh.strength:.2f}"
         f"  CLAHE {'ON' if enh.clahe_on else 'OFF'}  g {enh.gamma:.1f}", C_DIM),
        (f"SCAN  {'ON ' if scanner.enabled else 'OFF'} "
         f"{scanner.last.short(24) if scanner.last else 'waiting...'}", C_QR),
        (f"      {scanner.status()}", C_DIM),
        (f"DEV   {meta.node}", C_DIM),
    ]
    if show_ids:
        lines.append((f"SN    {util.ascii_safe(meta.serial) or 'N/A'}", C_ID))
        lines.append((f"INST  {util.ascii_safe(meta.install_id) or 'N/A'}", C_ID))
    lines.append((f"TIME  {util.now_stamp()}", C_WHITE))
    return lines
