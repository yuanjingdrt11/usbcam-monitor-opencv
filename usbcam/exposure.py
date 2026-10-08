#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""软件自动曝光 (AEC) 控制器。

策略（针对过曝光 + 稳定性）
--------------------------------------------------------------------------
* 控制输入取最近 N 帧中位数；每次调整后丢弃 settle 帧的陈旧画面，避免驱动滞后误判；
* 判定过曝只看高光像素占比(clip/sat)，不看 99.9 分位（画面里一小块灯就会污染分位）；
* 高光天花板记忆：记住最近一次造成溢出的曝光值，之后提亮最多到它的 92%，
  因此曝光单调收敛，不会出现"降过头->提回来->再降"的极限环；
* 只在画面确实偏暗时才缓慢释放天花板（默认 5s +5%），场景变化后仍能重新适应；
* 局部高光溢出用小步(×0.90)，整幅过曝(亮度>190 或溢出>30%)才大步快降；
* 高动态范围（逆光）保护：整体已偏暗仍溢出时停止降曝光并提示。
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from .stats import FrameStats
from .util import clamp


class ExposureController:
    def __init__(self, exp_min: int, exp_max: int,
                 target_clip: float = 0.5, clip_hi: float = 1.2, sat_hi: float = 2.4,
                 target_luma: float = 115.0, dark_luma: float = 75.0, min_luma: float = 45.0,
                 interval: float = 0.15, settle_frames: int = 2, ring: int = 3,
                 ceiling_hold: float = 5.0):
        self.exp_min = int(exp_min)
        self.exp_max = int(exp_max)
        self.target_clip = target_clip
        self.clip_hi = clip_hi
        self.sat_hi = sat_hi
        self.target_luma = target_luma
        self.dark_luma = dark_luma
        self.min_luma = min_luma
        self.interval = interval
        self.settle_frames = settle_frames
        self.ceiling_hold = ceiling_hold
        self._samples: deque = deque(maxlen=max(1, ring))
        self._skip = 0
        self.last_action = 0.0
        self.last_reason = "初始化"
        self.reason_en = "init"        # 窗口叠加层用（Hershey 字体不能渲染中文）
        self.converged = False
        self.limit_state = ""          # ""/"min"/"max"/"hdr"/"ceiling"
        self.adjust_count = 0
        self.ceiling: Optional[float] = None
        self.ceiling_time = 0.0

    # --- 供快捷键/参数变更时调用 ---
    def reconfigure(self, exp_min: Optional[int] = None, exp_max: Optional[int] = None) -> None:
        if exp_min is not None:
            self.exp_min = int(exp_min)
        if exp_max is not None:
            self.exp_max = int(exp_max)

    def reset(self) -> None:
        self._samples.clear()
        self._skip = 0
        self.converged = False
        self.limit_state = ""
        self.last_reason = "重置"
        self.reason_en = "reset"
        self.ceiling = None
        self.ceiling_time = 0.0

    # --- 每帧调用 ---
    def update(self, cur_raw: float, st: FrameStats, now: float) -> Optional[int]:
        """返回新的 raw 曝光值；无需调整返回 None。"""
        if self._skip > 0:
            self._skip -= 1
            return None

        self._samples.append((st.clip_pct, st.sat_pct, st.luma))
        clips = sorted(s[0] for s in self._samples)
        sats = sorted(s[1] for s in self._samples)
        lumas = sorted(s[2] for s in self._samples)
        mid = len(clips) // 2
        clip, sat, luma = clips[mid], sats[mid], lumas[mid]

        if now - self.last_action < self.interval:
            return None
        band_lo, band_hi = self.target_clip, self.clip_hi
        over = clip > band_hi or sat > self.sat_hi

        # 画面确实偏暗时缓慢释放高光天花板，场景变化后能重新提亮
        if (self.ceiling is not None and luma < self.dark_luma
                and now - self.ceiling_time > self.ceiling_hold):
            self.ceiling *= 1.05
            self.ceiling_time = now

        if over:
            if luma < self.min_luma:
                self.limit_state = "hdr"
                self.converged = True
                self.last_reason = (f"高动态范围: 整体偏暗({luma:.1f})且高光仍溢出"
                                    f"({clip:.2f}%)，停止降曝光")
                self.reason_en = f"HDR scene: keep exp (clip {clip:.2f}%)"
                return None
            severe = luma > 190.0 or clip > 30.0 or sat > 15.0
            factor = clamp(band_hi / max(clip, 1e-3), 0.40, 0.75) if severe else 0.90
            new = max(self.exp_min, min(self.exp_max, int(round(cur_raw * factor))))
            self.ceiling = cur_raw if self.ceiling is None else min(self.ceiling, cur_raw)
            self.ceiling_time = now
            self.last_reason = f"溢出{clip:.2f}% > {band_hi:.2f}% → 降曝光 x{factor:.2f}"
            self.reason_en = f"reduce exp x{factor:.2f} (clip {clip:.2f}%)"
            self.converged = False
        else:
            if clip <= band_lo * 0.6 and luma < self.dark_luma:
                factor = clamp(self.target_luma / max(luma, 1.0), 1.03, 1.25)
                why = f"偏暗{luma:.1f}"
            elif clip <= band_lo and luma < self.target_luma:
                factor = clamp(self.target_luma / max(luma, 1.0), 1.02, 1.08)
                why = f"未达亮度{luma:.1f}"
            else:
                self.converged = True
                self.limit_state = ""
                self.last_reason = "已收敛"
                self.reason_en = "converged"
                return None
            new = max(self.exp_min, min(self.exp_max, int(round(cur_raw * factor))))
            if self.ceiling is not None:
                cap = int(self.ceiling * 0.92)
                if new > cap:
                    new = max(self.exp_min, cap)
                    if new <= int(round(cur_raw)):
                        self.converged = True
                        self.limit_state = "ceiling"
                        self.last_reason = (f"已收敛: 高光余量用尽(上限raw{self.ceiling:.0f})，"
                                            f"亮度{luma:.1f}")
                        self.reason_en = f"highlight limit raw{self.ceiling:.0f} luma {luma:.0f}"
                        return None
            self.last_reason = f"{why} → 升曝光 x{factor:.2f}"
            self.reason_en = f"raise exp x{factor:.2f} (luma {luma:.0f})"
            self.converged = False

        self.last_action = now
        self._samples.clear()
        self._skip = self.settle_frames
        if new == int(round(cur_raw)):
            self.limit_state = "min" if over else "max"
            self.converged = True
            self.last_reason = "已达曝光极限，无法继续调整"
            self.reason_en = ("at MIN exposure, still clipped" if over
                              else "at MAX exposure, still dark")
            return None
        self.limit_state = ""
        self.adjust_count += 1
        return new

    # --- 状态提示语（控制台用，含处理建议） ---
    def advice(self) -> str:
        return {
            "max": "已达最大曝光仍偏暗：请增加照明，或用 --exposure-max-ms 放宽上限",
            "min": "已达最小曝光仍过曝：环境光过强，建议加 ND 减光片或降低光源亮度",
            "hdr": "高动态范围场景：整体偏暗但高光仍溢出，已停止降曝光以免主体过黑；"
                   "建议给高光区域补光或调整构图",
            "ceiling": "已收敛：曝光到达高光可用上限，希望更亮可加大 --clip-hi 或增加环境光",
        }.get(self.limit_state, "")
