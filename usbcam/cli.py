#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行参数。

默认值都是"策略"而不是"本机常量"：
* --size / --fps 默认 auto —— 由设备支持的模式列表现选，并实测校验；
* --power-line 默认 auto —— 按系统时区/区域推断 50/60Hz；
* 不出现任何设备节点号、序列号、PCI/USB 路径等本机标识。
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Optional

from .util import WINDOW_MAIN


def default_power_line() -> str:
    """按本机区域推断工频：中国/欧洲等 50Hz，美洲等 60Hz。"""
    tz = os.environ.get("TZ", "")
    try:
        tz = tz or (time.tzname[0] or "")
    except Exception:
        tz = ""
    try:
        import datetime
        off = datetime.datetime.now().astimezone().utcoffset()
        hours = off.total_seconds() / 3600 if off else 0
    except Exception:
        hours = 0
    # 粗略规则：-4~-10 时区（美洲）用 60Hz，其余用 50Hz；可用 --power-line 覆盖
    return "60" if -10 <= hours <= -3 else "50"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="usbcam",
        description="USB 相机过曝光优化 + 参数监视 + 扫码（OpenCV / V4L2）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"窗口名固定为 ASCII（'{WINDOW_MAIN}' / 'QR Result'）："
               f"中文窗口名在部分 OpenCV/X11 环境下无法创建。")

    # --- 设备 ---
    g = p.add_argument_group("设备")
    g.add_argument("--device", default="auto",
                   help="auto(默认,优先用记住的相机) | 索引 | /dev/videoN | "
                        "serial:XXX | name:XXX | install:XXX")
    g.add_argument("--list", action="store_true", help="列出采集设备与支持的模式后退出")
    g.add_argument("--set-default", action="store_true", help="把 --device 指定的相机记为默认后退出")
    g.add_argument("--forget", action="store_true", help="清除记住的默认相机/ROI 后退出")
    g.add_argument("--info-only", action="store_true", help="只读探测设备信息后退出（不改相机设置）")
    g.add_argument("--no-ask", action="store_true", help="多相机且无默认时不询问，直接用第一个")
    g.add_argument("--no-save-choice", action="store_true", help="不把本次 --device 选择记为默认")

    # --- 采集模式（默认自动协商） ---
    m = p.add_argument_group("采集模式（默认全部自动协商，不写死）")
    m.add_argument("--size", default="auto",
                   help="分辨率 auto(默认) 或 WxH，如 1280x720")
    m.add_argument("--fps", type=float, default=None, help="帧率上限（默认取设备支持的最高）")
    m.add_argument("--fourcc", default="auto", help="像素格式 auto(默认)/MJPG/YUYV")
    m.add_argument("--max-pixels", type=int, default=1280 * 720,
                   help="auto 选模式时画面像素上限（默认 1280x720，纯策略可改）")
    m.add_argument("--buffers", type=int, default=3,
                   help="V4L2 缓冲数（默认 3；设 1 会因每帧等新帧而帧率减半）")
    m.add_argument("--no-probe", action="store_true",
                   help="不做实测帧率校验（默认会实测，低于声明 80%% 时自动换模式）")
    m.add_argument("--benchmark", action="store_true",
                   help="实测该设备所有模式的真实帧率 + ROI 管线开销（有进度输出，"
                        "Ctrl+C 可中止并保留已测结果）")
    m.add_argument("--benchmark-seconds", type=float, default=0.6,
                   help="--benchmark 每个模式实测多少秒 (默认0.6)")
    m.add_argument("--fps-probe", action="store_true",
                   help="对当前模式请求 15/30/60/90/120fps 并实测实际帧率，"
                        "用来确认能否突破设备声明上限")
    m.add_argument("--no-thread", action="store_true",
                   help="关闭独立抓帧线程（默认开启：采集与处理解耦，跑满相机上限）")

    # --- ROI ---
    r = p.add_argument_group("ROI 切割（降低每帧计算量 / 只对感兴趣区域测光扫码）")
    r.add_argument("--roi", default=None,
                   help="x,y,w,h（像素）或 x,y,w,h%%（相对，如 25,25,50,50%%）；"
                        "auto=用上次记住的 ROI")
    r.add_argument("--no-roi", action="store_true", help="不使用 ROI（整幅处理）")
    r.add_argument("--no-hw-crop", action="store_true", help="不下发相机硬件裁剪（只用软件 ROI）")

    # --- 曝光 / 画质 ---
    e = p.add_argument_group("曝光与画质")
    e.add_argument("--exposure-ms", type=float, default=None, help="初始手动曝光(ms)，默认沿用当前值")
    e.add_argument("--exposure-min-ms", type=float, default=None, help="曝光下限(ms)")
    e.add_argument("--exposure-max-ms", type=float, default=None,
                   help="曝光上限(ms)，默认不超过一个帧周期")
    e.add_argument("--gain", type=int, default=None, help="增益（相机支持时；未指定则置 0）")
    e.add_argument("--keep-device-auto-exposure", action="store_true",
                   help="保留相机自带自动曝光（默认切手动，由 AEC 接管）")
    e.add_argument("--no-aec", action="store_true", help="关闭软件自动曝光 AEC")
    e.add_argument("--target-clip", type=float, default=0.5, help="目标高光溢出下限%% (默认0.5)")
    e.add_argument("--clip-hi", type=float, default=1.2, help="触发降曝光的溢出上限%% (默认1.2)")
    e.add_argument("--target-luma", type=float, default=115.0, help="目标平均亮度 (默认115)")
    e.add_argument("--dark-luma", type=float, default=75.0, help="低于该亮度才提亮 (默认75)")
    e.add_argument("--clip-threshold", type=int, default=250, help="判定高光溢出的灰度阈值 (默认250)")
    e.add_argument("--aec-interval", type=float, default=0.15, help="AEC 最小调整间隔秒 (默认0.15)")
    e.add_argument("--no-tone", action="store_true", help="关闭高光肩部压缩")
    e.add_argument("--tone-knee", type=float, default=0.78, help="肩部起点 0~1 (默认0.78)")
    e.add_argument("--tone-strength", type=float, default=0.6, help="压缩强度 0~1 (默认0.6)")
    e.add_argument("--gamma", type=float, default=1.0, help="伽马 (默认1.0)")
    e.add_argument("--clahe", action="store_true", help="开启 CLAHE 暗部提升（默认关闭）")
    e.add_argument("--power-line", default="auto", choices=["auto", "off", "50", "60"],
                   help="工频抗闪烁 auto(默认,按区域推断)/off/50/60")

    # --- 扫码 ---
    q = p.add_argument_group("扫码（默认开启）")
    q.add_argument("--no-qr", action="store_true", help="关闭扫码")
    q.add_argument("--qr-scan-width", type=int, default=640,
                   help="扫码检测所用的降采样宽度（默认640，越大越准越慢）")
    q.add_argument("--qr-interval", type=int, default=2,
                   help="每 N 帧扫一次 (默认2；后台线程执行，不占显示帧时间)")
    q.add_argument("--qr-cooldown", type=float, default=8.0, help="同一内容重复提示间隔秒 (默认8)")
    q.add_argument("--qr-popup", default="on", choices=["on", "off"],
                   help="扫到码是否弹窗 (默认on)")
    q.add_argument("--qr-popup-seconds", type=float, default=8.0, help="弹窗自动关闭秒数 (默认8)")
    q.add_argument("--qr-no-barcode", action="store_true", help="不识别一维条码（只扫二维码）")
    q.add_argument("--qr-sync", action="store_true",
                   help="扫码在主循环里同步执行（默认放后台线程，避免占用显示帧时间）")
    q.add_argument("--qr-effort", default="normal", choices=["fast", "normal", "deep"],
                   help="扫码力度 fast=只做一次检测 / normal=默认(经典检测器兜底) / "
                        "deep=再加多尺度+分块+预处理加强扫描")
    q.add_argument("--qr-dir", default="scans", help="扫码结果保存目录 (默认 scans/)")
    q.add_argument("--font", default=None, help="弹窗渲染中文用的字体文件（默认自动查找）")
    q.add_argument("--qr-open-url", action="store_true",
                   help="扫到 http(s) 链接时用浏览器打开（默认关闭，安全起见不自动打开）")

    # --- 界面 / 输出 ---
    o = p.add_argument_group("界面与输出")
    o.add_argument("--window-scale", type=float, default=1.0, help="显示缩放 (默认1.0)")
    o.add_argument("--no-gui", action="store_true", help="不创建窗口（无头运行）")
    o.add_argument("--duration", type=float, default=0.0, help="运行秒数，0=直到按 q (默认0)")
    o.add_argument("--status-interval", type=float, default=5.0, help="控制台状态行间隔秒 (默认5)")
    o.add_argument("--log-csv", default=None, help="把逐条统计写入 CSV")
    o.add_argument("--print-json", action="store_true", help="退出时打印设备信息 JSON")
    o.add_argument("--save-frame", default=None, help="启动约2秒后导出带叠加层的画面")
    o.add_argument("--snapshot-dir", default="snapshots", help="按 s 保存快照的目录")
    o.add_argument("--no-v4l2ctl", action="store_true", help="不使用 v4l2-ctl（纯 OpenCV）")
    o.add_argument("--keep-settings", action="store_true",
                   help="退出时保留优化后的相机设置（默认恢复运行前状态）")
    return p


def parse_size(spec: str) -> Optional[tuple]:
    if not spec or spec == "auto":
        return None
    try:
        w, h = spec.lower().replace("*", "x").split("x")
        return int(w), int(h)
    except Exception:
        raise SystemExit(f"[错误] --size 需要形如 1280x720 或 auto，收到: {spec}")
