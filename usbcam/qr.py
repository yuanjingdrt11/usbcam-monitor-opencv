#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫码（二维码 / 一维码）：默认开启，多尺度 + 预处理 + 候选跟踪。

为什么这样设计（实测数据，本机 OpenCV 5.0，1280x720 源）
--------------------------------------------------------------------------
    QRCodeDetectorAruco      480px 有码 5.2ms / 无码 7.3ms
                            1280px 有码 31.3ms / 无码 29.4ms
    QRCodeDetector           480px 有码 13.5ms / 无码 5.7ms
    barcode.BarcodeDetector  约 0.9~1.0ms
全分辨率每帧解码要 30ms 以上，会把帧率压垮；只做一次 480px 检测又容易漏掉
小码/糊码/低对比度码。所以采用分级策略：

    第 1 级（每 N 帧）       Aruco 检测器 @ scan_width（默认 480）
    第 2 级（每 N 帧）       经典检测器 @ scan_width（两者互补）
    第 3 级（命中四边形）    按该四边形裁出来做全分辨率精解（含 2x 放大）
    第 4 级（失败候选）      记住这个区域，后续若干帧持续精解（对焦/抖动时特别有效）
    第 5 级（deep，默认每 1s）CLAHE + 非锐化掩模预处理后再扫（低对比度/暗光）
    一维码（每 M 次）        BarcodeDetector，成本极低，一直开着

每次命中都会：控制台打印全文 + 弹窗（左码图右内容）+ 裁剪图与 JSONL 落盘。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from . import util
from .util import ascii_safe, has_cjk


# ---------------------------------------------------------------------------
@dataclass
class ScanResult:
    text: str
    kind: str                     # "QR" / "Barcode"
    points: np.ndarray            # Nx2，ROI 坐标系
    crop: np.ndarray              # 码区域裁剪图（BGR）
    stamp: str
    hits: int = 1
    scale: float = 1.0            # 命中时的检测缩放（调试用）
    stage: str = ""               # 命中的级别：fast/fast2/native/deep/track/barcode

    def short(self, n: int = 48) -> str:
        one = " ".join(self.text.split())
        return one if len(one) <= n else one[: n - 1] + "…"


def preprocess_contrast(bgr: np.ndarray) -> np.ndarray:
    """低对比度/暗光增强：CLAHE + 非锐化掩模，返回灰度图。"""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    g = clahe.apply(gray)
    blur = cv2.GaussianBlur(g, (0, 0), 2.0)
    return cv2.addWeighted(g, 1.6, blur, -0.6, 0)


class Scanner:
    """二维码/一维码识别器（分级策略 + 去重 + 候选跟踪）。"""

    def __init__(self, enabled: bool = True, scan_width: int = 640, interval_frames: int = 3,
                 cooldown: float = 8.0, barcode: bool = True, barcode_every: int = 4,
                 aruco: bool = True, effort: str = "normal", deep_every: float = 0.5,
                 native_max_pixels: int = 640 * 360):
        self.enabled = enabled
        self.scan_width = scan_width
        self.interval_frames = max(1, interval_frames)
        self.cooldown = cooldown
        self.effort = effort                  # fast / normal / deep
        self.deep_every = max(0.1, deep_every)
        self.native_max_pixels = native_max_pixels
        self._frame_idx = 0
        self._scans = 0
        self._seen: Dict[str, float] = {}
        self._last_deep = 0.0
        self._deep_rung = 0
        self._track: Optional[np.ndarray] = None      # 看到但没解出的区域
        self._track_left = 0
        self.history: List[ScanResult] = []
        self.last: Optional[ScanResult] = None
        self.last_time = 0.0
        self.last_cost_ms = 0.0
        self.stats = {"scans": 0, "deep": 0, "native": 0, "track": 0, "hits": 0}
        self._pending: List[ScanResult] = []

        self.primary = None
        self.secondary = None
        if enabled:
            if aruco and hasattr(cv2, "QRCodeDetectorAruco"):
                try:
                    self.primary = cv2.QRCodeDetectorAruco()
                except Exception:
                    self.primary = None
            if hasattr(cv2, "QRCodeDetector"):
                self.secondary = cv2.QRCodeDetector()
            if self.primary is None:
                self.primary, self.secondary = self.secondary, None
        self._bar = None
        self._barcode_every = max(1, barcode_every)
        if enabled and barcode and hasattr(cv2, "barcode"):
            try:
                self._bar = cv2.barcode.BarcodeDetector()
            except Exception:
                self._bar = None

    # --- 同步版的 submit/poll（后台线程版见 AsyncScanner，接口一致）---
    def submit(self, frame, now: Optional[float] = None) -> None:
        res = self.scan(frame, now)
        if res is not None:
            self._pending.append(res)

    def poll(self) -> Optional[ScanResult]:
        return self._pending.pop(0) if self._pending else None

    # --- 主入口 ---
    def scan(self, frame: np.ndarray, now: Optional[float] = None) -> Optional[ScanResult]:
        """对当前 ROI 图扫一次（内部自带节流）；返回"新出现"的扫码结果或 None。"""
        if not self.enabled or self.primary is None or frame is None or frame.size == 0:
            return None
        now = time.time() if now is None else now
        self._frame_idx += 1
        if self._frame_idx % self.interval_frames:
            return None
        self._scans += 1
        self.stats["scans"] += 1
        t0 = time.perf_counter()

        h, w = frame.shape[:2]
        scale = min(1.0, self.scan_width / float(max(1, w)))
        small = (cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))),
                            interpolation=cv2.INTER_AREA) if scale < 1.0 else frame)

        found = self._detect(self.primary, small, scale, "fast")
        if not found and self.secondary is not None and (
                self.effort != "fast" or self._scans % 3 == 0):
            found = self._detect(self.secondary, small, scale, "fast2")
        if not found and self._bar is not None and (self._scans % self._barcode_every == 0):
            found = self._detect_barcode(small, scale)
        if not found and self._track is not None and self._track_left > 0:
            self._track_left -= 1
            self.stats["track"] += 1
            found = self._decode_region(frame, self._track, "track")
        if not found and h * w <= self.native_max_pixels and scale < 1.0:
            self.stats["native"] += 1
            found = self._detect(self.primary, frame, 1.0, "native")
        if not found and self.effort != "fast" and (now - self._last_deep) >= self.deep_every:
            self._last_deep = now
            self.stats["deep"] += 1
            found = self._detect_deep(frame, scale)

        self.last_cost_ms = (time.perf_counter() - t0) * 1000.0
        if not found:
            return None
        text, quad, kind, stage, used_scale = found
        return self._handle(text, quad, kind, frame, used_scale, stage, now)

    # --- 各级检测 ---
    def _detect(self, det, img: np.ndarray, scale: float, stage: str):
        """单检测器：优先 detectAndDecodeMulti（一次可找到多个码），失败退回单码接口。"""
        if det is None or img is None or img.size == 0:
            return None
        texts: Sequence[str] = ()
        pts = None
        try:
            if hasattr(det, "detectAndDecodeMulti"):
                ok, texts, points, _ = det.detectAndDecodeMulti(img)
                if ok and points is not None and len(points):
                    pts = points
            if pts is None:
                text, p, _ = det.detectAndDecode(img)
                if text:
                    return (text, np.asarray(p, np.float32).reshape(-1, 2) / max(scale, 1e-6),
                            "QR", stage, scale)
                if p is not None and self._track is None:
                    self._remember_candidate(
                        np.asarray(p, np.float32).reshape(-1, 2) / max(scale, 1e-6))
                return None
        except cv2.error:
            return None
        for i, t in enumerate(texts):
            if t:
                quad = np.asarray(pts[i], np.float32).reshape(-1, 2) / max(scale, 1e-6)
                return t, quad, "QR", stage, scale
        if len(pts) and self._track is None:
            self._remember_candidate(
                np.asarray(pts[0], np.float32).reshape(-1, 2) / max(scale, 1e-6))
        return None

    def _detect_barcode(self, img: np.ndarray, scale: float):
        try:
            info, _types, pts = self._bar.detectAndDecode(img)
        except cv2.error:
            return None
        if info and info[0]:
            quad = (np.asarray(pts, np.float32).reshape(-1, 2) / max(scale, 1e-6)
                    if pts is not None else None)
            return info[0], quad, "Barcode", "barcode", scale
        return None

    def _detect_deep(self, frame: np.ndarray, scale: float):
        """
        加强扫描（deep）：把"多尺度 + 预处理 + 分块"排成一个梯子，每次 deep 只跑一级
        （轮转），这样单帧开销可控（约 5~30ms，不会卡住画面），一个完整轮转
        覆盖所有组合，专治"码小 / 糊 / 低对比 / 反光"这些漏检场景。
        """
        rungs = self._deep_rungs(frame)
        if not rungs:
            return None
        idx = self._deep_rung % len(rungs)
        self._deep_rung = (self._deep_rung + 1) % len(rungs)
        _tag, fn = rungs[idx]
        return fn()

    def _deep_rungs(self, frame: np.ndarray):
        """返回 [(说明, 调用)]，按代价从低到高排。"""
        h, w = frame.shape[:2]

        def scaled(target_w: int, pre: bool, where: str):
            def run():
                tw = int(min(max(target_w, 64), w))
                s = min(1.0, tw / float(max(1, w)))
                img = (cv2.resize(frame, (max(1, int(w * s)), max(1, int(h * s))),
                                  interpolation=cv2.INTER_AREA) if s < 1.0 else frame)
                if pre:
                    img = cv2.cvtColor(preprocess_contrast(img), cv2.COLOR_GRAY2BGR)
                for det in (self.primary, self.secondary):
                    got = self._detect(det, img, s, where)
                    if got:
                        return got
                return None
            return run

        def tiled(pre: bool):
            def run():
                for ox, oy, tile in self._tiles(frame):
                    th, tw = tile.shape[:2]
                    ts = min(1.0, self.scan_width / float(max(1, tw)))
                    img = (cv2.resize(tile, (max(1, int(tw * ts)), max(1, int(th * ts))),
                                      interpolation=cv2.INTER_AREA) if ts < 1.0 else tile)
                    if pre:
                        img = cv2.cvtColor(preprocess_contrast(img), cv2.COLOR_GRAY2BGR)
                    for det in (self.primary, self.secondary):
                        got = self._detect(det, img, ts, "deep-tile")
                        if got:
                            text, q, kind, st, sc = got
                            if q is not None:
                                q = (np.asarray(q, np.float32).reshape(-1, 2)
                                     + np.array([ox, oy], np.float32))
                            return text, q, kind, st, ts
                return None
            return run

        rungs = [
            ("1.5x", scaled(int(self.scan_width * 1.5), False, "deep")),
            ("1.5x+增强", scaled(int(self.scan_width * 1.5), True, "deep")),
            ("2x", scaled(int(self.scan_width * 2), False, "deep")),
            ("2x+增强", scaled(int(self.scan_width * 2), True, "deep")),
            ("分块", tiled(False)),
            ("分块+增强", tiled(True)),
        ]
        if h * w <= self.native_max_pixels:
            rungs.append(("原始分辨率", scaled(w, False, "native")))
        return rungs

    @staticmethod
    def _tiles(frame: np.ndarray, grid: int = 2, overlap: float = 0.2):
        """把画面切成 grid x grid 个带重叠的小块（小码在块内被相对放大）。"""
        h, w = frame.shape[:2]
        out = []
        for i in range(grid):
            for j in range(grid):
                x0 = max(0, int(j * w / grid - w * overlap / grid))
                y0 = max(0, int(i * h / grid - h * overlap / grid))
                x1 = min(w, int((j + 1) * w / grid + w * overlap / grid))
                y1 = min(h, int((i + 1) * h / grid + h * overlap / grid))
                if x1 - x0 >= 64 and y1 - y0 >= 64:
                    out.append((x0, y0, frame[y0:y1, x0:x1]))
        return out

    def _decode_region(self, frame: np.ndarray, quad: np.ndarray, stage: str):
        """把候选四边形裁出来，原分辨率 + 2x 放大各解一次。"""
        crop = self._crop(frame, quad, pad_ratio=0.25)
        if crop.size == 0 or min(crop.shape[:2]) < 12:
            return None
        tries = [crop]
        if max(crop.shape[:2]) < 400:
            tries.append(cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC))
        tries.append(cv2.cvtColor(preprocess_contrast(crop), cv2.COLOR_GRAY2BGR))
        for img in tries:
            for det in (self.primary, self.secondary):
                got = self._detect(det, img, 1.0, stage)
                if got:
                    text, q, kind, st, sc = got
                    return text, self._relocate(q, frame, quad), kind, st, sc
        return None

    @staticmethod
    def _relocate(q: Optional[np.ndarray], frame: np.ndarray, quad: np.ndarray) -> np.ndarray:
        """把"裁剪图坐标系"的四边形映射回整帧坐标系（裁剪原点近似）。"""
        if q is None:
            return np.asarray(quad, np.float32).reshape(-1, 2)
        h, w = frame.shape[:2]
        ox, oy = Scanner._crop_origin(frame, quad)
        out = np.asarray(q, np.float32).reshape(-1, 2) + np.array([ox, oy], np.float32)
        out[:, 0] = np.clip(out[:, 0], 0, w - 1)
        out[:, 1] = np.clip(out[:, 1], 0, h - 1)
        return out

    def _remember_candidate(self, quad: Optional[np.ndarray], ttl: int = 8) -> None:
        """记录"看到码但没解出"的区域，接下来几帧持续尝试精解。"""
        if quad is None or len(quad) < 4:
            return
        self._track = np.asarray(quad, np.float32).reshape(-1, 2)
        self._track_left = ttl

    # --- 结果处理 ---
    def _handle(self, text: str, quad, kind: str, frame: np.ndarray, scale: float,
                stage: str, now: float) -> Optional[ScanResult]:
        crop = self._crop(frame, quad)
        self._track, self._track_left = None, 0
        last_seen = self._seen.get(text)
        self._seen[text] = now
        if last_seen is not None and (now - last_seen) < self.cooldown:
            if self.last is not None and self.last.text == text:
                self.last.hits += 1
            return None
        res = ScanResult(text=text, kind=kind,
                         points=(np.asarray(quad, np.float32).reshape(-1, 2)
                                 if quad is not None else np.zeros((4, 2), np.float32)),
                         crop=crop, stamp=util.now_stamp(), scale=scale, stage=stage)
        self.last, self.last_time = res, now
        self.history.append(res)
        self.stats["hits"] += 1
        return res

    # --- 裁剪 ---
    @staticmethod
    def _crop_origin(frame: np.ndarray, pts: Optional[np.ndarray],
                     pad_ratio: float = 0.25) -> Tuple[int, int]:
        if pts is None or len(pts) < 4:
            return 0, 0
        h, w = frame.shape[:2]
        pts = np.asarray(pts, np.float32).reshape(-1, 2)
        x0, y0 = np.clip(pts.min(axis=0).astype(int), 0, [w - 1, h - 1])
        x1, y1 = np.clip(pts.max(axis=0).astype(int), 0, [w - 1, h - 1])
        pad = int(pad_ratio * max(x1 - x0, y1 - y0)) + 6
        return max(0, int(x0) - pad), max(0, int(y0) - pad)

    @staticmethod
    def _crop(frame: np.ndarray, pts: Optional[np.ndarray], pad_ratio: float = 0.12) -> np.ndarray:
        h, w = frame.shape[:2]
        if pts is None or len(pts) < 4:
            side = min(h, w)
            y0, x0 = (h - side) // 2, (w - side) // 2
            return frame[y0:y0 + side, x0:x0 + side].copy()
        pts = np.asarray(pts, np.float32).reshape(-1, 2)
        x0, y0 = np.clip(pts.min(axis=0).astype(int), 0, [w - 1, h - 1])
        x1, y1 = np.clip(pts.max(axis=0).astype(int), 0, [w - 1, h - 1])
        pad = int(pad_ratio * max(x1 - x0, y1 - y0)) + 6
        x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
        x1, y1 = min(w, x1 + pad), min(h, y1 + pad)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return frame.copy()
        return frame[y0:y1, x0:x1].copy()

    # --- 叠加层 ---
    def draw(self, img: np.ndarray) -> None:
        res = self.last
        if res is None or res.points is None or len(res.points) < 4:
            return
        if time.time() - self.last_time > self.cooldown:
            return
        pts = res.points.astype(int).reshape(-1, 1, 2)
        cv2.polylines(img, [pts], True, (0, 255, 0), 2)
        label = f"{res.kind}: {ascii_safe(res.short(40))}"
        cv2.rectangle(img, (0, 0), (min(img.shape[1], 18 + 11 * len(label)), 30), (0, 0, 0), -1)
        cv2.putText(img, label, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 1, cv2.LINE_AA)

    def status(self) -> str:
        """一行扫码统计，放进状态行/HUD，便于确认"到底有没有在工作"。"""
        s = self.stats
        return (f"扫{s['scans']}/命中{s['hits']} 深扫{s['deep']} 精解{s['native']} "
                f"跟踪{s['track']} {self.last_cost_ms:.1f}ms")


# ---------------------------------------------------------------------------
# 弹窗内容合成（PIL 渲染，可显示中文；无 PIL 时退回 ASCII）
# ---------------------------------------------------------------------------
def _pil_text_block(lines: List[Tuple[str, int, Tuple[int, int, int]]], width: int,
                    font_path: Optional[str], bg: Tuple[int, int, int] = (24, 24, 24)
                    ) -> Optional[np.ndarray]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    height = sum(size + 12 for _, size, _ in lines) + 16
    img = Image.new("RGB", (max(64, width), max(32, height)), tuple(reversed(bg)))
    draw = ImageDraw.Draw(img)
    y = 8
    for text, size, color in lines:
        font = None
        if font_path:
            try:
                font = ImageFont.truetype(font_path, size)
            except Exception:
                font = None
        if font is None:
            font = ImageFont.load_default()
        draw.text((10, y), text, font=font, fill=tuple(reversed(color)))
        y += size + 12
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def _cv_text_block(lines: List[Tuple[str, int, Tuple[int, int, int]]], width: int,
                   bg: Tuple[int, int, int] = (24, 24, 24)) -> np.ndarray:
    line_h = 22
    img = np.zeros((len(lines) * line_h + 16, max(64, width), 3), np.uint8)
    img[:] = bg
    y = 20
    for text, _size, color in lines:
        cv2.putText(img, ascii_safe(text, 120), (10, y), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, color, 1, cv2.LINE_AA)
        y += line_h
    return img


def compose_popup(res: ScanResult, device_lines: List[str], font_path: Optional[str] = None,
                  target_w: int = 900) -> np.ndarray:
    """左：二维码裁剪图；右：内容与设备信息。返回可直接 imshow 的 BGR 图。"""
    crop = res.crop if res.crop is not None and res.crop.size else np.full((120, 120, 3), 255, np.uint8)
    ch, cw = crop.shape[:2]
    side = 320
    scale = side / float(max(ch, cw))
    shown = cv2.resize(crop, (max(1, int(cw * scale)), max(1, int(ch * scale))),
                       interpolation=cv2.INTER_NEAREST if scale > 1 else cv2.INTER_AREA)
    panel = np.full((side + 24, side + 24, 3), 245, np.uint8)
    y0 = (panel.shape[0] - shown.shape[0]) // 2
    x0 = (panel.shape[1] - shown.shape[1]) // 2
    panel[y0:y0 + shown.shape[0], x0:x0 + shown.shape[1]] = shown

    right_w = max(320, target_w - panel.shape[1])
    wrap = max(18, right_w // 11)
    body_lines = util.wrap_text(res.text, wrap) if res.text else ["(空)"]
    lines: List[Tuple[str, int, Tuple[int, int, int]]] = [
        (f"{res.kind} DETECTED", 26, (120, 255, 120)),
        (f"{res.stamp}   [{res.stage}]", 16, (170, 170, 170)),
    ]
    for ln in body_lines[:12]:
        lines.append((ln, 22, (255, 255, 255)))
    if len(body_lines) > 12:
        lines.append((f"... (+{len(body_lines) - 12} lines)", 16, (170, 170, 170)))
    lines.append(("", 8, (0, 0, 0)))
    for ln in device_lines:
        lines.append((ln, 15, (150, 200, 255)))

    text_img = None
    if font_path or has_cjk(res.text):
        text_img = _pil_text_block(lines, right_w, font_path)
    if text_img is None:
        text_img = _cv_text_block([(ascii_safe(t), s, c) for t, s, c in lines], right_w)

    h = max(panel.shape[0], text_img.shape[0])
    out = np.full((h, panel.shape[1] + text_img.shape[1], 3), 24, np.uint8)
    out[:panel.shape[0], :panel.shape[1]] = panel
    out[:text_img.shape[0], panel.shape[1]:] = text_img
    cv2.line(out, (panel.shape[1], 0), (panel.shape[1], h), (80, 80, 80), 1)
    return out


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def save_result(res: ScanResult, out_dir: Path, meta: Dict[str, str]) -> Tuple[Path, Path]:
    """保存裁剪图 PNG 与 JSONL 记录，返回两个路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    img_path = out_dir / f"{res.kind.lower()}_{tag}.png"
    cv2.imwrite(str(img_path), res.crop)
    log_path = out_dir / "scans.jsonl"
    rec = {"timestamp": res.stamp, "kind": res.kind, "text": res.text,
           "image": img_path.name, "hits": res.hits, "stage": res.stage}
    rec.update(meta)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return img_path, log_path


# ---------------------------------------------------------------------------
class AsyncScanner:
    """
    后台线程扫码：主循环只负责"投递最新帧"和"取结果"，扫码开销不占显示帧时间。

    实测（本机 1280x720 整幅）：扫码每帧要 5~30ms（deep 档更多），放在主循环里
    会把处理上限压到 21fps；搬到后台线程后，显示与采集都按相机上限跑，
    扫码只受 CPU 空闲核心影响。
    """

    def __init__(self, scanner: Scanner, idle_sleep: float = 0.004):
        self.scanner = scanner
        self._lock = threading.Lock()
        self._frame = None
        self._seq = 0
        self._seen_seq = -1
        self._results: List[ScanResult] = []
        self._idle = idle_sleep
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not self.scanner.enabled:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="usbcam-qr", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def submit(self, frame) -> None:
        """交给后台扫（非阻塞）。frame 是只读的 ROI 视图，无人原地修改。"""
        if self._thread is None or frame is None:
            return
        with self._lock:
            self._frame = frame
            self._seq += 1

    def poll(self) -> Optional[ScanResult]:
        with self._lock:
            return self._results.pop(0) if self._results else None

    def _loop(self) -> None:
        while self._running:
            with self._lock:
                if self._seq == self._seen_seq or self._frame is None:
                    frame = None
                else:
                    frame = self._frame
                    self._seen_seq = self._seq
            if frame is None:
                time.sleep(self._idle)
                continue
            try:
                res = self.scanner.scan(frame, time.time())
            except Exception:                     # noqa: BLE001
                res = None
            if res is not None:
                with self._lock:
                    self._results.append(res)

    # --- 透传 ---
    @property
    def last(self):
        return self.scanner.last

    @property
    def enabled(self) -> bool:
        return self.scanner.enabled

    @property
    def stats(self) -> dict:
        return self.scanner.stats

    @property
    def last_cost_ms(self) -> float:
        return self.scanner.last_cost_ms

    def status(self) -> str:
        return self.scanner.status()

    def draw(self, img) -> None:
        self.scanner.draw(img)
