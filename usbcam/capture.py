#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集：打开设备、自动协商分辨率/帧率/像素格式、读帧、实测帧率、帧率上限探测。

要点
--------------------------------------------------------------------------
* 不写死分辨率和帧率：候选模式来自设备支持列表（v4l2-ctl --list-formats-ext），
  挑完**实测**帧率，明显低于声明就自动换下一个候选模式。
* 曝光上限按**实测帧率**折算（一帧周期），拿不到帧率时不额外限制 —— 不写死任何值。
* 缓冲数不能设 1：实测 BUFFERSIZE=1 时读操作每帧都要等新帧，帧率直接减半
  （1280x720 MJPG：1 缓冲 19.6fps -> 3 缓冲 30.0fps）。
* FrameGrabber 把抓帧放进独立线程，处理再慢也不会拖慢采集；相机控制
  （曝光/增益等）也统一在该线程执行，避免并发 ioctl。
* probe_fps() 用实测回答"能否突破设备声明的帧率上限"：
  UVC 描述符里每个帧描述符只给一个 dwFrameInterval 时，请求再高也会被夹回。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import cv2

from . import v4l2
from .v4l2 import Mode

V4L2_EXPOSURE_MANUAL = 1.0     # V4L2 menu: Manual Mode
V4L2_EXPOSURE_AUTO = 3.0       # V4L2 menu: Aperture Priority Mode


# ---------------------------------------------------------------------------
# 模式选择策略
# ---------------------------------------------------------------------------
def choose_mode(modes: List[Mode], fourcc: str = "auto",
                size: Optional[Tuple[int, int]] = None,
                fps: Optional[float] = None,
                max_pixels: int = 1280 * 720,
                prefer_fourcc: Tuple[str, ...] = ("MJPG", "YUYV")) -> Optional[Mode]:
    """
    在设备支持的模式里挑一个（全程不写死具体分辨率/帧率）：

      1. --size / --fourcc 是硬约束（指定了就只在其中挑）；
      2. 先按帧率优先：取最高帧率档（±5% 视为同档）；
      3. 同档里优先不超过 max_pixels 的画面（都超了就取最接近上限的）；
      4. 最后才用 prefer_fourcc 做软偏好（MJPG 优先，压缩传输、同分辨率下带宽更省）。
    """
    if not modes:
        return None
    cand = list(modes)

    hard_fourcc = bool(fourcc and fourcc != "auto")
    if hard_fourcc:
        picked = [m for m in cand if m.fourcc.upper() == fourcc.upper()]
        if picked:
            cand = picked

    if size:
        same = [m for m in cand if (m.width, m.height) == size]
        if same:
            cand = same
        else:                       # 请求的尺寸不支持：退到面积最接近的
            area = size[0] * size[1]
            best = min({(m.width, m.height) for m in cand},
                       key=lambda wh: abs(wh[0] * wh[1] - area))
            cand = [m for m in cand if (m.width, m.height) == best]

    if fps:
        # 帧率上限：在不超过上限的模式里取最高档；一个都不满足才退到最低档
        within = [m for m in cand if m.fps <= fps * 1.05]
        cand = within if within else [min(cand, key=lambda m: m.fps)]
    top = max(m.fps for m in cand)
    fast = [m for m in cand if m.fps >= top * 0.95]

    fitting = [m for m in fast if m.pixels <= max_pixels]
    pool = fitting if fitting else [min(fast, key=lambda m: m.pixels)]

    if not hard_fourcc:             # 格式只是软偏好，让位给"像素上限"
        for fc in prefer_fourcc:
            picked = [m for m in pool if m.fourcc.upper() == fc]
            if picked:
                pool = picked
                break
    return max(pool, key=lambda m: m.pixels)


# ---------------------------------------------------------------------------
# 相机
# ---------------------------------------------------------------------------
@dataclass
class CaptureReport:
    node: str
    mode: Mode
    measured_fps: float
    buffers: int
    requested: str
    note: str = ""


class Camera:
    """一个采集设备的生命周期封装（纯 OpenCV + 可选 v4l2-ctl 兜底）。"""

    def __init__(self, node: str, fourcc: str = "auto", size: Optional[Tuple[int, int]] = None,
                 fps: Optional[float] = None, buffers: int = 3,
                 max_pixels: int = 1280 * 720, use_v4l2ctl: bool = True,
                 auto_probe: bool = True):
        self.node = node
        self.want_fourcc = fourcc
        self.want_size = size
        self.want_fps = fps
        self.buffers = buffers
        self.max_pixels = max_pixels
        self.auto_probe = auto_probe
        self.ctl = v4l2.Controls(node, enabled=use_v4l2ctl)
        self.cap: Optional[cv2.VideoCapture] = None
        self.modes: List[Mode] = []
        self.mode: Optional[Mode] = None
        self.measured_fps: float = 0.0
        self.exp_min, self.exp_max, self.exp_step = 1, 10000, 1
        self.has_gain = False

    # --- 打开与协商 ---
    def open(self) -> bool:
        self.modes = v4l2.list_modes(self.node)
        self.mode = choose_mode(self.modes, self.want_fourcc, self.want_size,
                                self.want_fps, self.max_pixels)
        cands: List[Mode] = [self.mode] if self.mode else []
        if self.auto_probe and self.modes and not self.want_size and not self.want_fps:
            rest = [m for m in self.modes if m not in cands]
            rest.sort(key=lambda m: (-m.fps, -m.pixels))
            cands += rest[:2]                      # 备选：实测不达标时自动换

        for mode in cands:
            if mode is None:
                continue
            if not self._open_one(mode):
                continue
            fps = self.measure_fps(0.6)
            declared = mode.fps or 0
            if declared and fps < 0.8 * declared and mode is not cands[-1]:
                self.release()
                continue
            self.measured_fps = fps
            self._read_limits()
            return True
        self.release()
        return False

    def _open_one(self, mode: Mode) -> bool:
        cap = cv2.VideoCapture(self.node, cv2.CAP_V4L2)
        if not cap.isOpened():
            return False
        # 先定格式再定尺寸，部分 UVC 相机顺序反了会协商失败
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*mode.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(mode.width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(mode.height))
        # 用户明确要了帧率就按用户的下发（驱动会夹到最接近的支持档），否则用模式自带的
        target_fps = self.want_fps or mode.fps
        if target_fps:
            cap.set(cv2.CAP_PROP_FPS, float(target_fps))
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, float(self.buffers))
        except Exception:
            pass
        got = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        if got != (mode.width, mode.height):
            # 协商不到就退回设备实际给的分辨率，不硬套
            mode = Mode(mode.fourcc, got[0], got[1], mode.fps)
        self.cap = cap
        self.mode = mode
        for _ in range(5):
            cap.read()
        return True

    def measure_fps(self, seconds: float = 0.6) -> float:
        if self.cap is None:
            return 0.0
        n, t0 = 0, time.time()
        while time.time() - t0 < seconds:
            ok, _ = self.cap.read()
            n += 1 if ok else 0
        dt = time.time() - t0
        return n / dt if dt > 0 else 0.0

    def _read_limits(self) -> None:
        if self.ctl.available:
            self.ctl.refresh()
        self.exp_min = self.ctl.limit("exposure_time_absolute", "min", 1) or 1
        self.exp_max = self.ctl.limit("exposure_time_absolute", "max", 10000) or 10000
        self.exp_step = self.ctl.limit("exposure_time_absolute", "step", 1) or 1
        self.has_gain = self.ctl.has("gain")

    def release(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    # --- 读帧 ---
    def read(self) -> Tuple[bool, Optional["cv2.typing.MatLike"]]:  # type: ignore[name-defined]
        if self.cap is None:
            return False, None
        return self.cap.read()

    # --- 格式信息 ---
    def resolution(self) -> Tuple[int, int]:
        if self.cap is None:
            return (0, 0)
        return (int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))

    def fourcc(self) -> str:
        if self.cap is None:
            return ""
        v = int(self.cap.get(cv2.CAP_PROP_FOURCC))
        return "".join(chr((v >> (8 * i)) & 0xFF) for i in range(4))

    def declared_fps(self) -> float:
        return float(self.mode.fps) if self.mode else 0.0

    # --- 曝光 ---
    def exposure_raw(self) -> float:
        return float(self.cap.get(cv2.CAP_PROP_EXPOSURE)) if self.cap is not None else 0.0

    def set_exposure_raw(self, raw: float) -> float:
        """写曝光并回读（驱动可能按帧周期截断）；返回实际生效值。"""
        if self.cap is None:
            return 0.0
        raw = int(round(min(max(raw, self.exp_min), self.exp_max)))
        ok = self.cap.set(cv2.CAP_PROP_EXPOSURE, float(raw))
        got = self.exposure_raw()
        if (not ok or abs(got - raw) > 1.5) and self.ctl.has("exposure_time_absolute"):
            if self.ctl.set("exposure_time_absolute", raw):
                got = float(self.ctl.get("exposure_time_absolute") or raw)
        return got

    def set_auto_exposure(self, auto: bool) -> bool:
        if self.cap is None:
            return False
        want = V4L2_EXPOSURE_AUTO if auto else V4L2_EXPOSURE_MANUAL
        self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, want)
        got = self.cap.get(cv2.CAP_PROP_AUTO_EXPOSURE)
        if abs(got - want) > 0.5 and self.ctl.has("auto_exposure"):
            self.ctl.set("auto_exposure", int(want))
            got = float(self.ctl.get("auto_exposure") or want)
        return abs(got - want) < 0.5

    def auto_exposure_enabled(self) -> bool:
        if self.cap is None:
            return False
        return self.cap.get(cv2.CAP_PROP_AUTO_EXPOSURE) not in (V4L2_EXPOSURE_MANUAL, 0.0)

    def gain(self) -> Optional[int]:
        if self.cap is None:
            return None
        v = self.cap.get(cv2.CAP_PROP_GAIN)
        if v is None or v < 0:
            return self.ctl.get("gain") if self.ctl.has("gain") else None
        return int(v)

    def set_gain(self, val: int) -> bool:
        if self.cap is None:
            return False
        ok = self.cap.set(cv2.CAP_PROP_GAIN, float(val))
        if not ok and self.ctl.has("gain"):
            ok = self.ctl.set("gain", val)
        return bool(ok)

    def set_auto_wb(self, on: bool) -> bool:
        return bool(self.cap.set(cv2.CAP_PROP_AUTO_WB, 1.0 if on else 0.0)) \
            if self.cap is not None else False

    # --- 与帧率相关的曝光上限 ---
    def max_exposure_raw(self, ratio: float = 0.95) -> int:
        """曝光不能超过一个帧周期，否则必然掉帧。

        帧率只认实测值（其次设备声明值）；两者都拿不到时不设额外上限，
        直接交给相机自己的 exp_max —— 绝不写死一个 30fps 当真值用。
        """
        fps = self.measured_fps or self.declared_fps()
        if fps <= 0:
            return int(self.exp_max)
        return int(min(self.exp_max, ratio * (1e6 / fps) / 100.0))


def measure_mode(node: str, mode: Mode, seconds: float = 0.6) -> Optional[float]:
    """实测单个模式的真实交付帧率；该模式打不开或尺寸协商不到则返回 None。"""
    cap = cv2.VideoCapture(node, cv2.CAP_V4L2)
    if not cap.isOpened():
        return None
    try:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*mode.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(mode.width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(mode.height))
        cap.set(cv2.CAP_PROP_FPS, float(mode.fps))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 3)
        got = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        if got != (mode.width, mode.height):
            return None
        for _ in range(4):
            cap.read()
        n, t0 = 0, time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            ok, _ = cap.read()
            n += 1 if ok else 0
        dt = max(time.perf_counter() - t0, 1e-6)
        return n / dt
    finally:
        cap.release()


def dedupe_modes(modes: List[Mode]) -> List[Mode]:
    """去掉设备重复上报的模式（同一 fourcc/尺寸/帧率会出现多次）。"""
    seen = set()
    out: List[Mode] = []
    for m in modes:
        key = (m.fourcc.upper(), m.width, m.height, round(m.fps, 3))
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out


def mode_table(node: str, seconds: float = 0.6, modes: Optional[List[Mode]] = None,
               progress=None) -> List[Tuple[Mode, float]]:
    """
    逐个实测模式真实帧率。progress(done, total, mode, fps) 可用于打印进度，
    避免长时间无输出让人以为卡死；调用方可随时 Ctrl+C，已测结果仍然有效。
    """
    todo = dedupe_modes(modes if modes is not None else v4l2.list_modes(node))
    out: List[Tuple[Mode, float]] = []
    for i, mode in enumerate(todo, 1):
        fps = measure_mode(node, mode, seconds)
        if progress is not None:
            progress(i, len(todo), mode, fps)
        if fps is not None:
            out.append((mode, fps))
    return out


# ---------------------------------------------------------------------------
# 帧率上限：来自设备描述符的事实，以及"能不能突破"的实测
# ---------------------------------------------------------------------------
def declared_ceiling(modes: List[Mode]) -> Tuple[float, List[Mode]]:
    """返回 (设备声明的最高帧率, 达到该帧率的模式列表)。"""
    if not modes:
        return 0.0, []
    top = max(m.fps for m in modes)
    return top, [m for m in modes if abs(m.fps - top) < 1e-6]


def ceiling_note(modes: List[Mode]) -> str:
    """一句话说明帧率上限及其来源（UVC 描述符里的 dwFrameInterval）。"""
    top, best = declared_ceiling(modes)
    if top <= 0:
        return "无法读取设备声明的帧率上限"
    sizes = ", ".join(sorted({f"{m.width}x{m.height}" for m in best})[:6])
    return (f"设备声明上限 {top:g} fps（UVC 描述符里所有帧描述符给出的最快帧间隔），"
            f"出现在: {sizes}。超过该值的请求会被驱动夹回，属于相机固件限制。")


def probe_fps(node: str, mode: Mode, requests: Sequence[float],
              seconds: float = 0.8) -> List[Tuple[float, float, float]]:
    """
    对同一个模式请求不同帧率，实测实际拿到的帧率。

    返回 [(请求fps, 驱动接受值, 实测fps)]。用来回答"能不能突破上限"：
    如果请求 60 实测还是 30，说明相机固件就是 30fps（描述符只给了一个帧间隔）。
    """
    out: List[Tuple[float, float, float]] = []
    for want in requests:
        cap = cv2.VideoCapture(node, cv2.CAP_V4L2)
        if not cap.isOpened():
            break
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*mode.fourcc))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(mode.width))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(mode.height))
            cap.set(cv2.CAP_PROP_FPS, float(want))
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 3)
            got = float(cap.get(cv2.CAP_PROP_FPS))
            for _ in range(4):
                cap.read()
            n, t0 = 0, time.perf_counter()
            while time.perf_counter() - t0 < seconds:
                ok, _ = cap.read()
                n += 1 if ok else 0
            dt = max(time.perf_counter() - t0, 1e-6)
            out.append((want, got, n / dt))
        finally:
            cap.release()
    return out


class FrameGrabber:
    """
    独立线程持续抓帧：主循环永远拿"最新一帧"，处理慢也不会拖慢采集。

    为什么需要：单线程里 read() 会阻塞到下一帧，一旦处理耗时超过帧周期，
    实际帧率就掉到相机上限以下（实测 BUFFERSIZE=1 时 30fps 掉到 19.6fps；
    同步回读曝光时处理上限被压到 21fps）。抓帧线程把两件事解耦后，采集侧稳定
    跑满相机上限，处理侧能跑多快跑多快，同时还能准确统计采集帧率与丢帧数。
    相机控制也统一在这个线程里执行（V4L2 单线程化，避免并发 ioctl）。
    """

    def __init__(self, cam: "Camera"):
        self.cam = cam
        self._lock = threading.Lock()
        self._frame = None
        self._seq = 0
        self._fps = 0.0
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._fails = 0
        self._queue: "queue.Queue" = queue.Queue()
        self._exp = 0.0

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="usbcam-grabber", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        last = time.perf_counter()
        while self._running:
            while True:                      # 先执行主线程排进来的相机操作
                try:
                    self._queue.get_nowait()()
                except queue.Empty:
                    break
                except Exception:            # noqa: BLE001
                    pass
            ok, frame = self.cam.read()
            now = time.perf_counter()
            dt = now - last
            last = now
            if not ok or frame is None:
                self._fails += 1
                time.sleep(0.005)
                continue
            self._fails = 0
            if dt > 0:
                self._fps = self._fps * 0.9 + (1.0 / dt) * 0.1 if self._fps else 1.0 / dt
            try:                             # 顺便缓存曝光值：主线程读取零成本、无需往返
                self._exp = self.cam.exposure_raw()
            except Exception:                # noqa: BLE001
                pass
            with self._lock:
                self._frame = frame
                self._seq += 1

    # --- 主循环接口 ---
    def latest(self):
        with self._lock:
            return self._frame, self._seq

    def fps(self) -> float:
        return self._fps

    def fails(self) -> int:
        return self._fails

    def exposure(self) -> float:
        """当前曝光（抓帧线程每帧刷新，主线程直接读缓存）。"""
        return self._exp

    def post(self, fn) -> None:
        """非阻塞地把相机操作排给抓帧线程（不等结果，避免主循环卡一个帧周期）。"""
        self._queue.put(fn)

    def call(self, fn, timeout: float = 2.0):
        """阻塞版：需要立即拿结果时用（会等抓帧线程下一次循环）。"""
        box: dict = {}
        done = threading.Event()

        def job() -> None:
            try:
                box["r"] = fn()
            except Exception as e:           # noqa: BLE001
                box["e"] = e
            finally:
                done.set()

        self._queue.put(job)
        if not done.wait(timeout):
            return None
        if "e" in box:
            raise box["e"]
        return box.get("r")
