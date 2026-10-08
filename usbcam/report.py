#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制台输出：设备报告、状态行、运行统计、CSV/JSON。

控制台是 UTF-8 终端，可以放心用中文；窗口叠加层则必须 ASCII（见 overlay.py）。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional, TextIO

from . import util
from .capture import Camera
from .devices import DeviceMeta
from .enhance import Enhancer
from .exposure import ExposureController
from .stats import FrameStats


def rule(char: str = "-", n: int = 68) -> str:
    return char * n


def device_report(meta: DeviceMeta, cam: Camera, measured_fps: float,
                  auto_exp: bool, roi_label: str, qr_note: str) -> None:
    w, h = cam.resolution()
    mode = cam.mode.label() if cam.mode else "N/A"
    print(rule())
    print("USB 相机设备信息 / 采集参数")
    print(rule())
    print(f"  时间戳 Timestamp        : {util.now_stamp()}")
    print(f"  设备节点 Device Node    : {meta.node}  (OpenCV index {meta.index})")
    print(f"  设备名称 Model          : {meta.name or 'N/A'}")
    print(f"  序列号 Serial Number    : {meta.serial_or_na()}")
    print(f"  物理安装 ID Install ID  : {meta.install_or_na()}")
    if meta.usb_path:
        print(f"  物理端口拓扑 USB Path   : {meta.usb_path}")
    print(f"  厂商/产品 ID VID:PID    : {meta.vid_pid or 'N/A'}"
          f"   ({meta.manufacturer or 'N/A'} / {meta.product or 'N/A'})")
    print(f"  驱动 Driver             : {meta.driver or 'N/A'}   总线: {meta.bus_info or 'N/A'}")
    print(f"  分辨率 Resolution       : {w}x{h}  ({cam.fourcc()})")
    print(f"  采集模式 Mode           : {mode}   (设备声明 {cam.declared_fps():.1f} fps)")
    print(f"  帧率 FPS                : 实测 {measured_fps:.1f} fps   "
          f"缓冲 {cam.buffers}")
    print(f"  曝光 Exposure           : {'AUTO(设备自动)' if auto_exp else 'MANUAL(手动)'}"
          f"  {cam.exposure_raw() / util.RAW_PER_MS:.2f} ms (raw {cam.exposure_raw():.0f})")
    print(f"  增益 Gain               : {'N/A(不支持)' if cam.gain() is None else cam.gain()}")
    print(f"  ROI                     : {roi_label}")
    print(f"  扫码                    : {qr_note}")
    print(rule())


def status(meta: DeviceMeta, cam: Camera, fps_cap: float, fps_proc: float,
           exp_raw: float, st: FrameStats, ctrl: ExposureController, enh: Enhancer,
           aec_on: bool, auto_exp: bool, roi_label: str, qr_note: str,
           dropped: int = 0) -> None:
    flags = (f"AEC={'ON' if aec_on else 'OFF'} TONE={'ON' if enh.tone else 'OFF'} "
             f"CLAHE={'ON' if enh.clahe_on else 'OFF'} G={enh.gamma:.1f} "
             f"DEVEXP={'AUTO' if auto_exp else 'MANUAL'}")
    print(f"[状态] {util.now_stamp()} | SN={meta.serial_or_na()} | ID={meta.install_or_na()} | "
          f"{cam.resolution()[0]}x{cam.resolution()[1]} ROI={roi_label} | "
          f"帧率 采集{fps_cap:.1f}/处理{fps_proc:.1f}"
          + (f"(丢{dropped})" if dropped else "") + " | "
          f"曝光 {exp_raw / util.RAW_PER_MS:.2f}ms | 增益 "
          f"{cam.gain() if cam.gain() is not None else 'N/A'} | "
          f"溢出 {st.clip_pct:.2f}%(死白 {st.sat_pct:.2f}%) | 亮度 {st.luma:.1f} | "
          f"{flags} | QR={qr_note} | AEC: {ctrl.last_reason}")


class CsvLog:
    """逐条记录状态，便于事后分析过曝/帧率随时间的变化。"""

    FIELDS = ["timestamp", "node", "serial", "install_id", "width", "height", "roi",
              "fps_capture", "fps_process", "exposure_ms", "exposure_raw", "gain",
              "clip_pct", "saturated_pct", "luma", "p999", "aec", "auto_exposure",
              "tone", "clahe", "gamma", "qr_last"]

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh: Optional[TextIO] = open(self.path, "w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.fh)
        self.writer.writerow(self.FIELDS)

    def write(self, meta: DeviceMeta, cam: Camera, fps_cap: float, fps_proc: float,
              exp_raw: float, st: FrameStats, aec_on: bool, auto_exp: bool,
              enh: Enhancer, roi_label: str, qr_last: str) -> None:
        if self.fh is None:
            return
        w, h = cam.resolution()
        self.writer.writerow([
            util.now_stamp(), meta.node, meta.serial, meta.install_id, w, h, roi_label,
            f"{fps_cap:.2f}", f"{fps_proc:.2f}", f"{exp_raw / util.RAW_PER_MS:.3f}",
            f"{exp_raw:.0f}", cam.gain(), f"{st.clip_pct:.3f}", f"{st.sat_pct:.3f}",
            f"{st.luma:.2f}", f"{st.p999:.0f}", int(aec_on), int(auto_exp),
            int(enh.tone), int(enh.clahe_on), enh.gamma, qr_last])

    def close(self) -> None:
        if self.fh is not None:
            self.fh.close()
            self.fh = None


def summary(meta: DeviceMeta, cam: Camera, frames: int, fails: int, duration: float,
            exp_min: float, exp_max: float, exp_end: float, clip_start: float,
            clip_end: float, scans: int, log_csv: Optional[str], json_out: bool,
            final: Optional[dict] = None, fps_proc: float = 0.0,
            extra: Optional[dict] = None) -> None:
    # 相机可能已释放，优先用释放前抓取的快照
    final = final or {}
    w, h = final.get("res") or cam.resolution()
    fourcc = final.get("fourcc") or cam.fourcc()
    mode_label = final.get("mode") or (cam.mode.label() if cam.mode else "N/A")
    print(rule("=", 68))
    print("运行统计")
    print(f"  时间戳 Timestamp      : {util.now_stamp()}")
    print(f"  序列号 Serial Number  : {meta.serial_or_na()}")
    print(f"  物理安装 ID Install ID: {meta.install_or_na()}")
    print(f"  分辨率 Resolution     : {w}x{h} ({fourcc})  模式 {mode_label}")
    print(f"  帧率 FPS              : 采集 {cam.measured_fps:.1f} / 循环实际 "
          f"{frames / max(duration, 1e-6):.1f} / 处理上限 {fps_proc:.1f}"
          f"  (设备声明 {cam.declared_fps():.1f})")
    print(f"  帧数 Frames           : {frames}，读取失败 {fails}")
    print(f"  曝光范围 Exposure     : {exp_min / util.RAW_PER_MS:.2f} ~ "
          f"{exp_max / util.RAW_PER_MS:.2f} ms (结束时 {exp_end / util.RAW_PER_MS:.2f} ms)")
    print(f"  高光溢出 Clipped      : 开始 {clip_start:.2f}% -> 结束 {clip_end:.2f}%")
    print(f"  扫码次数 Scans        : {scans}")
    if log_csv:
        print(f"  统计日志 CSV          : {log_csv}")
    if json_out:
        data = meta.as_dict()
        data.update({
            "timestamp": util.now_stamp(),
            "resolution": f"{w}x{h}",
            "pixel_format": fourcc,
            "mode": mode_label,
            "measured_fps": round(cam.measured_fps, 2),
            "process_ceiling_fps": round(fps_proc, 2),
            "declared_fps": cam.declared_fps(),
            "frames": frames,
            "exposure_ms_end": round(exp_end / util.RAW_PER_MS, 3),
            "clip_pct_start": round(clip_start, 3),
            "clip_pct_end": round(clip_end, 3),
            "scans": scans,
        })
        if extra:
            data.update(extra)
        print(json.dumps(data, ensure_ascii=False, indent=2))
    print(rule("=", 68))
