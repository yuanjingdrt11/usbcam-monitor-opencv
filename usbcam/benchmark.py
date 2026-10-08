#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基准测试：实测各采集模式的真实帧率，以及 ROI 对每帧处理开销的影响。

用数据回答两个问题：
    1. 这台相机的采集帧率上限在哪（哪些模式声明 30 实际只有 10）；
    2. 把处理区域从整幅缩到 ROI，能省多少处理时间、处理上限能提到多少。
"""

from __future__ import annotations

import sys
import time
from typing import List, Tuple

import numpy as np

from . import qr, v4l2
from .capture import Camera, ceiling_note, dedupe_modes, measure_mode, probe_fps
from .cli import parse_size
from .devices import DeviceMeta
from .v4l2 import Mode
from .enhance import Enhancer
from .stats import analyse


def run(node: str, meta: DeviceMeta, args) -> int:
    """
    模式基准 + ROI 管线基准。

    两个都会实测，但模式表逐个模式打开相机，20 个模式约 15 秒；
    因此这里打印进度、支持 --benchmark-seconds 调节每档测量时长，
    并且 Ctrl+C 时会保留已测到的结果而不是丢一个 traceback。
    """
    secs = max(0.2, float(getattr(args, "benchmark_seconds", 0.6)))
    modes = dedupe_modes(v4l2.list_modes(node))
    print("=" * 78)
    print(f"模式基准测试: {node}  {meta.name}")
    print("=" * 78)
    print(ceiling_note(modes))
    print(f"\n共 {len(modes)} 个模式（已去重），每档实测 {secs:g} 秒 —— "
          f"约需 {len(modes) * (secs + 0.4):.0f} 秒，Ctrl+C 可随时中止并保留已测结果\n")

    table: List[Tuple[Mode, float]] = []
    try:
        for i, mode in enumerate(modes, 1):
            print(f"  [{i:>2}/{len(modes)}] {mode.label():<24}", end="", flush=True)
            fps = measure_mode(node, mode, secs)
            if fps is None:
                print("跳过（该模式打不开或尺寸协商失败）")
                continue
            table.append((mode, fps))
            flag = ("  <-- 高于声明" if fps > mode.fps * 1.05
                    else ("  <-- 明显低于声明" if fps < mode.fps * 0.8 else ""))
            print(f"声明 {mode.fps:>5.1f}  实测 {fps:>5.1f} fps{flag}")
    except KeyboardInterrupt:
        print("\n\n[中断] 已停止模式测试，下面用已测到的结果继续。")

    if not table:
        print("\n未测到任何可用模式。")
    else:
        print("-" * 78)
        print(f"{'模式':<24}{'声明fps':>10}{'实测fps':>10}{'达成率':>9}")
        for mode, fps in sorted(table, key=lambda t: (t[0].fourcc, -t[0].pixels)):
            rate = f"{fps / mode.fps * 100:.0f}%" if mode.fps else "-"
            print(f"{mode.label():<24}{mode.fps:>10.1f}{fps:>10.1f}{rate:>9}")
        best = max(table, key=lambda t: t[1])
        print("-" * 78)
        print(f"实测最快: {best[0].label()} -> {best[1]:.1f} fps"
              f"   （auto 策略会自动选到这一档）")
    print("说明: 采集帧率上限由相机固件/模式决定（见上面 ceiling 说明）；")
    print("      ROI 不改变采集上限，它降低每帧处理量，提升处理上限、降低延迟。")

    # ---- 管线基准（重新打开相机）----
    try:
        return _pipeline_bench(node, meta, args, secs)
    except KeyboardInterrupt:
        print("\n[中断] 已停止管线基准。")
        return 0


def _pipeline_bench(node: str, meta: DeviceMeta, args, secs: float) -> int:
    cam = Camera(node, fourcc=args.fourcc, size=parse_size(args.size), fps=args.fps,
                 buffers=args.buffers, max_pixels=args.max_pixels,
                 use_v4l2ctl=not args.no_v4l2ctl, auto_probe=not args.no_probe)
    if not cam.open():
        print("\n[错误] 无法打开相机做管线基准（可能被其他程序占用）", file=sys.stderr)
        return 3
    ok, frame = cam.read()
    if not ok or frame is None:
        print("\n[错误] 读取画面失败", file=sys.stderr)
        cam.release()
        return 4

    h, w = frame.shape[:2]
    enh = Enhancer(tone=True)
    scanner = qr.Scanner(enabled=not args.no_qr, scan_width=args.qr_scan_width,
                         interval_frames=1, cooldown=args.qr_cooldown,
                         barcode=not args.qr_no_barcode)

    def bench(fn, n: int = 30) -> float:
        fn()
        t0 = time.perf_counter()
        for _ in range(n):
            fn()
        return (time.perf_counter() - t0) / n * 1000.0

    print("\n" + "-" * 78)
    print(f"{'区域':<18}{'像素':>9}{'统计':>8}{'增强':>8}{'扫码/次':>9}{'扫码摊薄':>9}"
          f"{'主循环合计':>11}{'显示上限':>10}")
    regions: List[Tuple[str, int, int, int, int]] = [
        ("整幅", 0, 0, w, h),
        ("ROI 1/2 边长", w // 4, h // 4, w // 2, h // 2),
        ("ROI 1/4 边长", w // 8 * 3, h // 8 * 3, w // 4, h // 4),
    ]
    for name, x, y, rw, rh in regions:
        if rw < 32 or rh < 32:
            continue
        img: np.ndarray = frame[y:y + rh, x:x + rw]
        t_stats = bench(lambda: analyse(img, args.clip_threshold))
        t_enh = bench(lambda: enh.apply(img))
        t_qr = bench(lambda: scanner.scan(img, time.time()), n=12)
        qr_amort = t_qr / max(1, args.qr_interval)      # 后台线程 + 每 N 帧一次
        loop_ms = t_stats + t_enh + (qr_amort if args.qr_sync else 0.0)
        print(f"{name + ' ' + str(rw) + 'x' + str(rh):<18}{rw * rh:>9}"
              f"{t_stats:>7.2f}m{t_enh:>7.2f}m{t_qr:>8.2f}m{qr_amort:>8.2f}m"
              f"{loop_ms:>10.2f}m{1000 / max(loop_ms, 1e-3):>9.1f}fps")
    print("-" * 78)
    print(f"采集侧: {cam.mode.label() if cam.mode else 'N/A'} 实测 {cam.measured_fps:.1f} fps；"
          f"扫码工作分辨率 {args.qr_scan_width}px，每 {args.qr_interval} 帧一次，"
          f"{'同步(占主循环)' if args.qr_sync else '后台线程(不占主循环)'}")
    print("  扫码/次 = 单次扫码耗时；扫码摊薄 = 单次/间隔；显示上限 = 主循环(统计+增强"
          + ("+扫码摊薄" if args.qr_sync else "，不含后台扫码") + ")能跑到多少 fps")
    if cam.measured_fps > 0:
        print(f"结论: 实际帧率 = min(采集上限 {cam.measured_fps:.0f} fps, 显示上限)，"
              f"两者取小。")
    cam.release()
    return 0


def run_fps_probe(node: str, meta: DeviceMeta, mode, args) -> int:
    """请求一串帧率并实测：回答"能不能突破设备声明的上限"。"""
    print("=" * 74)
    print(f"帧率探测: {node}  {meta.name}")
    print("=" * 74)
    modes = v4l2.list_modes(node)
    print(ceiling_note(modes))
    if mode is None:
        print("[错误] 没有可用模式", file=sys.stderr)
        return 3
    print(f"\n当前模式: {mode.label()}（声明 {mode.fps:g} fps）")
    print(f"{'请求fps':>10}{'驱动接受':>12}{'实测fps':>12}   结论")
    best = 0.0
    for want, granted, got in probe_fps(node, mode, [15, 30, 45, 60, 90, 120], seconds=0.8):
        best = max(best, got)
        if got > mode.fps * 1.05:
            note = "突破成功，实际高于声明"
        elif want < mode.fps * 0.95 and got > want * 1.05:
            note = "该模式只有这一个帧间隔，无法降帧"
        elif got >= mode.fps * 0.95:
            note = "跑满声明上限"
        else:
            note = "低于声明上限"
        print(f"{want:>10.0f}{granted:>12.1f}{got:>12.1f}   {note}")
    print("-" * 74)
    if best > mode.fps * 1.05:
        print(f"结论: 该相机可以突破声明值，实测最高 {best:.1f} fps —— "
              f"可用 --fps {best:.0f} 让它跑起来。")
    else:
        print(f"结论: 请求再高也被夹回 {mode.fps:g} fps。原因在相机固件：")
        print("      UVC 描述符里每个帧描述符只给出一个 dwFrameInterval（=1/帧率），")
        print("      主机无法请求更短间隔，因此这是硬件上限，软件（含 ROI 裁剪）无法突破。")
        print("      能提升的是处理侧：ROI 降低每帧计算量、抓帧线程保证采集不被处理拖慢。")
    return 0
