#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主循环装配：把设备、采集、ROI、曝光、增强、扫码、叠加层、输出串起来。"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import List, Optional

import cv2

from . import config, devices, qr, report, roi as roi_mod, util
from .capture import Camera, FrameGrabber, ceiling_note
from .cli import build_parser, default_power_line, parse_size
from .enhance import Enhancer, highlight_clipped, side_by_side
from .exposure import ExposureController
from .stats import FrameStats, analyse
from .util import WINDOW_MAIN, WINDOW_QR
from . import overlay as ov


# ---------------------------------------------------------------------------
# 只读信息 / 模式基准
# ---------------------------------------------------------------------------
def info_only(cam: Camera, meta: devices.DeviceMeta, args) -> int:
    for _ in range(6):
        cam.read()
    ok, frame = cam.read()
    st = analyse(frame, args.clip_threshold) if ok and frame is not None else FrameStats()
    report.device_report(meta, cam, cam.measured_fps, cam.auto_exposure_enabled(),
                         "整幅 (探测模式)", "未启用 (--info-only)")
    print(f"  高光溢出 Clipped        : {st.clip_pct:.2f}% (阈值 >= {args.clip_threshold})"
          f"   平均亮度 {st.luma:.1f}   P99.9 {st.p999:.0f}")
    if args.print_json:
        import json
        data = meta.as_dict()
        data.update({"timestamp": util.now_stamp(), "resolution": f"{cam.resolution()[0]}x"
                     f"{cam.resolution()[1]}", "pixel_format": cam.fourcc(),
                     "mode": cam.mode.label() if cam.mode else "",
                     "declared_fps": cam.declared_fps(),
                     "measured_fps": round(cam.measured_fps, 2),
                     "exposure_ms": round(cam.exposure_raw() / util.RAW_PER_MS, 3),
                     "auto_exposure": cam.auto_exposure_enabled(), "gain": cam.gain(),
                     "clip_pct": round(st.clip_pct, 3), "luma": round(st.luma, 2)})
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.forget:
        ok = config.clear()
        print(f"[配置] {'已清除' if ok else '清除失败'}: 默认相机与 ROI 记忆")
        return 0
    if args.list:
        return devices.list_report()

    metas = [devices.read_meta(n) for n in devices.capture_nodes()]
    chosen, why = devices.choose(args.device, metas, allow_ask=not args.no_ask)
    if chosen is None:
        print(f"[错误] 无法选择相机: {why}", file=sys.stderr)
        devices.list_report()
        return 2
    meta, node = chosen, chosen.node
    print(f"[设备] 使用 {node}  {meta.name}   （选择来源: {why}）")
    if len(metas) > 1:
        others = ", ".join(m.node for m in metas if m.node != node)
        print(f"[设备] 另有: {others}   —— 如不是你要的相机，运行后按 n 切换"
              f"（--list 可查看，--device 可指定）")
    if args.set_default:
        if why in ("命令行 --device", "交互选择"):
            devices.save_choice(meta)
            return 0
        print(f"[设备] 当前自动选中的是 {node} {meta.name}（来源: {why}）；"
              f"如需设为默认请用 --device 明确指定后再加 --set-default。")
        return 0
    if why in ("命令行 --device", "交互选择") and not args.no_save_choice:
        devices.save_choice(meta)

    cam = Camera(node, fourcc=args.fourcc, size=parse_size(args.size), fps=args.fps,
                 buffers=args.buffers, max_pixels=args.max_pixels,
                 use_v4l2ctl=not args.no_v4l2ctl, auto_probe=not args.no_probe)
    if not cam.open():
        print(f"[错误] 打开/协商相机失败: {node}"
              f"（可能被其他程序占用，检查 fuser -v {node}）", file=sys.stderr)
        return 3
    print(f"[采集] 帧率上限: {ceiling_note(cam.modes)}")
    print(f"[采集] 模式 {cam.mode.label() if cam.mode else 'N/A'}  "
          f"实测 {cam.measured_fps:.1f} fps  缓冲 {cam.buffers}"
          + ("" if not cam.mode or not cam.mode.fps else
             f"  (设备声明 {cam.mode.fps:g} fps)"))
    if cam.mode and cam.mode.fps and cam.measured_fps < 0.8 * cam.mode.fps:
        print("[警告] 实测帧率明显低于声明，可尝试 --max-pixels 更小、"
              "--fourcc YUYV/MJPG 互换、提高 --buffers，或换 USB 端口/线缆。")

    if args.benchmark:
        cam.release()                       # 基准测试要独占设备
        from . import benchmark
        return benchmark.run(node, meta, args)
    if args.fps_probe:
        cam.release()
        from . import benchmark
        return benchmark.run_fps_probe(node, meta, cam.mode, args)

    if args.exposure_min_ms is not None:
        cam.exp_min = max(1, int(round(args.exposure_min_ms * util.RAW_PER_MS)))
    if args.exposure_max_ms is not None:
        cam.exp_max = int(round(args.exposure_max_ms * util.RAW_PER_MS))

    # ---- 只读探测 ----
    if args.info_only:
        rc = info_only(cam, meta, args)
        cam.release()
        return rc

    # ---- 记录原始状态，退出时恢复 ----
    orig = {
        "auto_exposure": cam.auto_exposure_enabled(),
        "exposure": cam.exposure_raw(),
        "gain": cam.gain(),
        "power_line": cam.ctl.get("power_line_frequency") if cam.ctl.available else None,
        "dyn_fps": cam.ctl.get("exposure_dynamic_framerate") if cam.ctl.available else None,
    }

    # ---- 设备侧优化 ----
    pl = args.power_line
    if pl == "auto":
        pl = default_power_line()
        print(f"[配置] 工频抗闪烁: 按本机区域推断为 {pl}Hz（可用 --power-line 覆盖）")
    if pl != "off":
        want = 1 if pl == "50" else 2
        if cam.ctl.set("power_line_frequency", want):
            print(f"[配置] 工频抗闪烁 power_line_frequency = {pl}Hz")
        else:
            print("[配置] 工频抗闪烁: 该设备不支持或缺少 v4l2-ctl，已跳过")
    if cam.ctl.set("exposure_dynamic_framerate", 0):
        print("[配置] 已关闭曝光动态降帧，保证帧率稳定")

    auto_exp = args.keep_device_auto_exposure
    if auto_exp:
        print("[配置] 保留相机自动曝光（强光下可能仍过曝，AEC 无法完全接管）")
    else:
        if cam.set_auto_exposure(False):
            print("[配置] 已切换为手动曝光 (auto_exposure=Manual)")
        else:
            print("[警告] 无法关闭相机自动曝光，AEC 仍会尝试调节曝光值")
        auto_exp = cam.auto_exposure_enabled()

    if args.exposure_ms is not None:
        got = cam.set_exposure_raw(args.exposure_ms * util.RAW_PER_MS)
        print(f"[配置] 初始曝光 {args.exposure_ms:.2f} ms -> 实际 {got / util.RAW_PER_MS:.2f} ms")
    else:
        print(f"[配置] 初始曝光沿用当前值 {cam.exposure_raw() / util.RAW_PER_MS:.2f} ms")
    if args.gain is not None:
        print(f"[配置] 增益设 {args.gain}: {'成功' if cam.set_gain(args.gain) else '失败(不支持)'}")
    elif cam.has_gain and cam.ctl.limit("gain", "min", 0) == 0 and cam.ctl.limit("gain", "max", 0) > 0:
        if cam.set_gain(0):
            print("[配置] 增益已归零：降低噪声与高光过早饱和")
    cam.set_auto_wb(True)

    for _ in range(6):
        cam.read()

    frame_w, frame_h = cam.resolution()

    # ---- ROI ----
    roi_enabled = not args.no_roi
    cur_roi: Optional[roi_mod.Roi] = None
    if roi_enabled:
        if args.roi and args.roi != "auto":
            cur_roi = roi_mod.Roi.parse(args.roi, frame_w, frame_h)
            if cur_roi is None:
                print(f"[警告] 无法解析 --roi '{args.roi}'（示例 320,180,640,360 或 25,25,50,50%）")
        elif args.roi != "auto" or True:
            cur_roi = roi_mod.load(frame_w, frame_h)      # 用上次记住的
            if cur_roi is not None and cur_roi.is_full(frame_w, frame_h):
                cur_roi = None
    roi_label = cur_roi.label() if cur_roi else f"整幅 {frame_w}x{frame_h}"
    if cur_roi is None:
        full_roi = roi_mod.Roi.full(frame_w, frame_h)
    else:
        full_roi = cur_roi
    print(f"[ROI ] {roi_label}"
          + ("" if cur_roi else "（按 z 可框选，o 临时切换整幅/ROI，自动记住）"))
    if cur_roi is not None:
        print(f"[ROI ] {roi_mod.try_hardware_crop(node, cur_roi, frame_w, frame_h, not args.no_hw_crop)}")

    # ---- 处理器 ----
    exp_cap = cam.max_exposure_raw()
    if args.exposure_max_ms is not None:
        exp_cap = min(cam.exp_max, int(round(args.exposure_max_ms * util.RAW_PER_MS)))
    ctrl = ExposureController(cam.exp_min, exp_cap, target_clip=args.target_clip,
                              clip_hi=args.clip_hi, target_luma=args.target_luma,
                              dark_luma=args.dark_luma, interval=args.aec_interval)
    aec_on = not args.no_aec
    enh = Enhancer(tone=not args.no_tone, clahe=args.clahe, knee=args.tone_knee,
                   strength=args.tone_strength, gamma=args.gamma)
    base_scanner = qr.Scanner(enabled=not args.no_qr, scan_width=args.qr_scan_width,
                              interval_frames=args.qr_interval, cooldown=args.qr_cooldown,
                              barcode=not args.qr_no_barcode, effort=args.qr_effort)
    scanner = base_scanner if args.qr_sync else qr.AsyncScanner(base_scanner)
    qr_note = (f"开启 力度={args.qr_effort} 工作宽度 {args.qr_scan_width}px, "
               f"每 {args.qr_interval} 帧扫一次, 去重 {args.qr_cooldown:g}s, "
               + ("同步(占主循环)" if args.qr_sync else "后台线程(不占显示帧)")
               + (", 含一维码" if base_scanner._bar else "")) if scanner.enabled else "关闭 (--no-qr)"
    print(f"[配置] 曝光范围 raw {cam.exp_min}~{exp_cap} "
          f"({cam.exp_min / util.RAW_PER_MS:.1f}~{exp_cap / util.RAW_PER_MS:.1f} ms), "
          f"AEC={'ON' if aec_on else 'OFF'} | 高光压缩={'ON' if enh.tone else 'OFF'} "
          f"knee={enh.knee} x{enh.strength} | CLAHE={'ON' if enh.clahe_on else 'OFF'}")
    print(f"[扫码] {qr_note}")

    font_path = util.find_cjk_font(args.font)
    if scanner.enabled:
        print(f"[扫码] 弹窗字体: {font_path or '未找到中文字体，将退回 ASCII 显示'}")

    # ---- 首帧 + 设备报告 ----
    st = FrameStats()
    first = None
    for _ in range(30):
        ok, f = cam.read()
        if ok and f is not None:
            first = f
            break
    if first is None:
        print("[错误] 无法读取画面帧", file=sys.stderr)
        cam.release()
        return 4
    st = analyse(full_roi.apply(first), args.clip_threshold)
    report.device_report(meta, cam, cam.measured_fps, auto_exp,
                         roi_label, qr_note)

    # ---- 首轮收敛 ----
    if aec_on:
        t0 = time.time()
        while time.time() - t0 < 3.0:
            ok, f = cam.read()
            if not ok or f is None:
                continue
            st = analyse(full_roi.apply(f), args.clip_threshold)
            new = ctrl.update(cam.exposure_raw(), st, time.time())
            if new is not None:
                cam.set_exposure_raw(new)
            elif ctrl.converged:
                break
        print(f"[AEC ] 收敛: 曝光 {cam.exposure_raw() / util.RAW_PER_MS:.2f} ms, "
              f"溢出 {st.clip_pct:.2f}%, 亮度 {st.luma:.1f}")

    # ---- 窗口 ----
    use_gui = not args.no_gui
    if use_gui:
        try:
            cv2.namedWindow(WINDOW_MAIN, cv2.WINDOW_AUTOSIZE)
        except cv2.error as e:
            print(f"[警告] 无法创建窗口({e})，转为无头模式。"
                  f"请确认 DISPLAY（当前 {os.environ.get('DISPLAY')}）", file=sys.stderr)
            use_gui = False

    # ---- 采集线程：把"抓帧"和"处理"解耦，处理再慢也不拖慢采集 ----
    grabber: Optional[FrameGrabber] = None
    if not args.no_thread:
        grabber = FrameGrabber(cam)
        grabber.start()
        print("[采集] 已启用独立抓帧线程：采集侧跑满相机上限，处理再慢也不掉采集帧")
    else:
        print("[采集] 单线程模式（--no-thread）：处理耗时会计入帧周期")

    def cam_call(fn, default=None):
        """相机操作统一走抓帧线程（V4L2 单线程化）。"""
        if grabber is None:
            try:
                return fn()
            except Exception:
                return default
        try:
            return grabber.call(fn)
        except Exception:
            return default

    if isinstance(scanner, qr.AsyncScanner):
        scanner.start()
        print("[扫码] 已启用后台扫码线程：扫码耗时不再占用显示帧时间")

    selector = roi_mod.RoiSelector()

    def _on_mouse(event: int, x: int, y: int, flags: int, param) -> None:
        inv = 1.0 / max(disp_scale[0], 1e-6)
        selector.on_mouse(event, int(x * inv), int(y * inv), flags, param)

    disp_scale = [1.0]
    if use_gui:
        cv2.setMouseCallback(WINDOW_MAIN, _on_mouse)

    # ---- 运行状态 ----
    show_help, show_ids, compare, show_clip, paused = True, True, False, False, False
    popup_until = 0.0
    popup_open = False
    switch_to_next = False
    save_frame_path = args.save_frame
    next_csv = last_status = last_aec_log = time.time()
    last_limit = ""
    t_start = last_t = time.time()
    # 只认实测值，其次设备声明值；都不写死（拿不到就显示 0，由 HUD 标 n/a）
    fps_cap = cam.measured_fps or cam.declared_fps() or 0.0
    fps_proc = 0.0
    frames = fails = scans = 0
    exp_min_seen = exp_max_seen = cam.exposure_raw()
    exp_cache: Optional[float] = None
    proc_ema = 0.0
    last_seq = 0
    dropped = 0
    clip_start = clip_end = st.clip_pct
    csv_log = report.CsvLog(args.log_csv) if args.log_csv else None

    print(f"[运行] 窗口 '{WINDOW_MAIN}'；按 q/Esc 退出，h 查看快捷键。")
    if not use_gui:
        print(f"[运行] 无头模式，运行 {args.duration if args.duration > 0 else '直到 Ctrl+C'}"
              f"{' 秒' if args.duration > 0 else ''}")

    exit_code = 0
    final = {"res": (frame_w, frame_h), "fourcc": "", "mode": "",
             "exp": cam.exposure_raw()}
    try:
        while True:
            if args.duration > 0 and (time.time() - t_start) >= args.duration:
                break
            frame, seq = grabber.latest() if grabber is not None else (None, 0)
            if grabber is None:
                ok, frame = cam.read()
                seq += 1 if ok else 0
            now = time.time()
            if frame is None or seq == last_seq:
                # 还没有新帧：保持窗口响应按键，不做处理
                if grabber is not None:
                    fails = grabber.fails()
                    fps_cap = grabber.fps()
                    if fails > 60:
                        print("[错误] 连续读取失败过多，退出", file=sys.stderr)
                        exit_code = 4
                        break
                if use_gui:
                    cv2.waitKey(1)
                else:
                    time.sleep(0.002)
                continue
            dropped += max(0, seq - last_seq - 1)
            last_seq = seq
            fails = 0
            frames += 1
            if grabber is not None:
                fps_cap = grabber.fps()
            else:
                dt = now - last_t
                last_t = now
                if dt > 0:
                    fps_cap = 0.9 * fps_cap + 0.1 / dt
            declared = cam.declared_fps()
            at_ceiling = bool(declared and fps_cap >= 0.97 * declared)
            t_proc0 = time.perf_counter()      # 处理耗时（不含阻塞等帧）

            active_roi = full_roi if roi_enabled else roi_mod.Roi.full(frame_w, frame_h)
            img = active_roi.apply(frame)
            st = analyse(img, args.clip_threshold)

            # ---- AEC ----
            exp = grabber.exposure() if grabber is not None else cam.exposure_raw()
            exp = exp or exp_cache or 0.0
            if aec_on and not paused:
                new = ctrl.update(exp, st, now)
                if new is not None:
                    # 非阻塞下发：真正的生效值下一帧由抓帧线程回读（驱动可能夹到帧周期）
                    if grabber is not None:
                        grabber.post(lambda v=new: cam.set_exposure_raw(v))
                    else:
                        cam.set_exposure_raw(new)
                    exp_cache = float(new)
                    if now - last_aec_log >= 1.0:
                        last_aec_log = now
                        print(f"[AEC ] {util.now_stamp()} 曝光 -> {exp / util.RAW_PER_MS:.2f} ms "
                              f"(raw {exp:.0f}) | {ctrl.last_reason}")
                elif ctrl.limit_state and ctrl.limit_state != last_limit:
                    last_limit = ctrl.limit_state
                    advice = ctrl.advice()
                    if advice:
                        print(f"[AEC ] {util.now_stamp()} {advice}")
            new_roi = selector.take()
            if new_roi is not None:
                full_roi = new_roi if not new_roi.is_full(frame_w, frame_h) else full_roi
                roi_enabled = True
                roi_mod.save(full_roi, frame_w, frame_h)
                roi_label = full_roi.label()
                print(f"[ROI ] 已框选 {roi_label} 并记住；"
                      f"{roi_mod.try_hardware_crop(node, full_roi, frame_w, frame_h, not args.no_hw_crop)}")
            exp_min_seen, exp_max_seen = min(exp_min_seen, exp), max(exp_max_seen, exp)
            clip_end = st.clip_pct

            # ---- 图像增强 ----
            proc = enh.apply(img)

            # ---- 扫码（后台线程时这里只投递与取结果，几乎零成本）----
            scanner.submit(img)
            res = scanner.poll()
            if res is not None:
                scans += 1
                print(f"[扫码] {res.stamp}  类型={res.kind}  "
                      f"SN={meta.serial_or_na()}  ID={meta.install_or_na()}")
                for line in (res.text or "").splitlines() or [""]:
                    print(f"       内容| {line}")
                try:
                    img_path, log_path = qr.save_result(
                        res, Path(args.qr_dir),
                        {"node": meta.node, "serial": meta.serial,
                         "install_id": meta.install_id,
                         "resolution": f"{frame_w}x{frame_h}", "roi": roi_label})
                    print(f"       已保存: {img_path}  (记录 {log_path})")
                except Exception as e:
                    print(f"[警告] 扫码结果落盘失败: {e}", file=sys.stderr)
                if args.qr_popup == "on" and use_gui:
                    try:
                        popup = qr.compose_popup(
                            res, [f"Device: {meta.node}  {util.ascii_safe(meta.name, 28)}",
                                  f"SN: {util.ascii_safe(meta.serial_or_na(), 32)}",
                                  f"ID: {util.ascii_safe(meta.install_or_na(), 32)}",
                                  f"ROI: {roi_label}"], font_path)
                        cv2.namedWindow(WINDOW_QR, cv2.WINDOW_AUTOSIZE)
                        cv2.imshow(WINDOW_QR, popup)
                        popup_until = now + args.qr_popup_seconds
                        popup_open = True
                    except cv2.error as e:
                        print(f"[警告] 扫码弹窗创建失败: {e}", file=sys.stderr)
                if res.text.startswith(("http://", "https://")) and args.qr_open_url:
                    try:
                        import subprocess
                        subprocess.Popen(["xdg-open", res.text], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
                        print(f"       已在浏览器打开: {res.text}")
                    except Exception as e:
                        print(f"[警告] 打开链接失败: {e}", file=sys.stderr)

            # ---- 显示 ----
            need_overlay = use_gui or save_frame_path is not None
            disp = proc
            if need_overlay:
                if selector.active:
                    disp = frame.copy()                      # 框选时显示整幅，便于选区域
                    if roi_enabled:
                        roi_mod.draw_roi_box(disp, full_roi)
                elif compare:
                    disp = side_by_side(img, proc)
                if show_clip and not selector.active:
                    disp = highlight_clipped(disp, args.clip_threshold)
                if cur_roi is not None and not roi_enabled and not selector.active:
                    roi_mod.draw_roi_box(disp, full_roi)

                if use_gui and args.window_scale != 1.0:
                    disp = cv2.resize(disp, None, fx=args.window_scale, fy=args.window_scale,
                                      interpolation=cv2.INTER_AREA)
                disp_scale[0] = (disp.shape[1] / max(frame_w, 1)) if selector.active else 1.0

                lines = ov.hud_lines(meta, cam, fps_cap, fps_proc, img, exp, st, ctrl, enh,
                                     aec_on, auto_exp, roi_label, scanner, paused, show_ids,
                                     dropped=dropped, at_ceiling=at_ceiling)
                ov.panel(disp, lines, align="right", scale=0.52)
                if show_help:
                    ov.help_panel(disp)
                if aec_on and paused:
                    ov.banner(disp, "AEC PAUSED (image live)")
                scanner.draw(disp) if not selector.active else None
                selector.draw(disp)
            if use_gui:
                cv2.imshow(WINDOW_MAIN, disp)
                if popup_open and now >= popup_until:
                    try:
                        cv2.destroyWindow(WINDOW_QR)
                    except cv2.error:
                        pass
                    popup_open = False
            if save_frame_path is not None and frames >= 60:
                Path(save_frame_path).parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(save_frame_path, disp)
                print(f"[导出] 已保存带叠加层的画面: {save_frame_path}")
                save_frame_path = None

            # ---- 处理耗时 -> 处理帧率上限（和采集帧率分开看，ROI 的收益体现在这里）----
            # 先对"每帧处理耗时"做平滑再取倒数：扫码是每隔几帧才有的大开销，
            # 直接平均 1/耗时 会被快帧带偏，平均耗时才反映真实处理上限
            proc_s = max(time.perf_counter() - t_proc0, 1e-6)
            proc_ema = proc_s if proc_ema <= 0 else 0.9 * proc_ema + 0.1 * proc_s
            fps_proc = 1.0 / proc_ema

            # ---- 状态 / CSV ----
            if now - last_status >= args.status_interval:
                last_status = now
                report.status(meta, cam, fps_cap, fps_proc, exp, st, ctrl, enh, aec_on,
                              auto_exp, roi_label, scanner.status(), dropped)
            if csv_log and now >= next_csv:
                next_csv = now + max(0.2, args.status_interval)
                csv_log.write(meta, cam, fps_cap, fps_proc, exp, st, aec_on, auto_exp, enh,
                              roi_label, scanner.last.text if scanner.last else "")

            # ---- 按键 ----
            if not use_gui:
                time.sleep(0.001)
                continue
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                if selector.active:
                    selector.cancel()
                    continue
                break
            elif key == ord("a"):
                aec_on = not aec_on
                ctrl.reset()
                print(f"[按键] AEC {'ON' if aec_on else 'OFF'}")
            elif key == ord("t"):
                enh.tone = not enh.tone
                print(f"[按键] 高光肩部压缩 {'ON' if enh.tone else 'OFF'}")
            elif key == ord("c"):
                enh.clahe_on = not enh.clahe_on
                print(f"[按键] CLAHE {'ON' if enh.clahe_on else 'OFF'}")
            elif key == ord("b"):
                compare = not compare
            elif key == ord("k"):
                show_clip = not show_clip
            elif key == ord("i"):
                show_ids = not show_ids
            elif key == ord("h"):
                show_help = not show_help
            elif key == ord("p"):
                paused = not paused
            elif key == ord("o"):
                roi_enabled = not roi_enabled
                print(f"[按键] ROI {'开启 ' + roi_label if roi_enabled else '关闭(整幅)'}")
            elif key == ord("z"):
                if selector.active:
                    selector.cancel()
                    print("[按键] 已取消框选")
                else:
                    selector.active = True
                    print("[按键] 请在窗口中拖拽框选 ROI（Esc 取消）")
            elif key == ord("n"):
                switch_to_next = True
                print("[按键] 切换到下一个相机…")
                break
            elif key == ord("e"):
                auto_exp = not auto_exp
                (grabber.post(lambda v=auto_exp: cam.set_auto_exposure(v)) if grabber is not None
                 else cam.set_auto_exposure(auto_exp))
                if auto_exp:
                    aec_on = False
                print(f"[按键] 设备自动曝光 {'ON (AEC 关闭)' if auto_exp else 'OFF'}")
            elif key in (ord("+"), ord("="), ord("-"), ord("_")):
                step = 1.0 if key in (ord("+"), ord("=")) else -1.0
                aec_on = False
                cur = exp_cache or (grabber.exposure() if grabber is not None else cam.exposure_raw())
                want = cur + step * util.RAW_PER_MS
                if grabber is not None:
                    grabber.post(lambda v=want: cam.set_exposure_raw(v))
                else:
                    cam.set_exposure_raw(want)
                exp_cache = want
                print(f"[按键] 手动曝光 -> {got / util.RAW_PER_MS:.2f} ms (AEC 关闭)")
            elif key == ord("g"):
                gammas = [1.0, 0.9, 0.8, 1.1, 1.2]
                i = gammas.index(enh.gamma) if enh.gamma in gammas else 0
                enh.gamma = gammas[(i + 1) % len(gammas)]
                enh.rebuild()
                print(f"[按键] gamma = {enh.gamma}")
            elif key == ord("r"):
                ctrl.reset()
                (grabber.post(lambda: cam.set_auto_exposure(False)) if grabber is not None
                 else cam.set_auto_exposure(False))
                auto_exp, aec_on = False, True
                print("[按键] 已重置 AEC 重新收敛")
            elif key == ord("y"):
                scanner._frame_idx = 0
                scanner._seen.clear()
                print("[按键] 已清除扫码去重记录，立即重新扫码")
            elif key == ord("s"):
                d = Path(args.snapshot_dir)
                d.mkdir(parents=True, exist_ok=True)
                tag = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
                p1, p2 = d / f"raw_{tag}.png", d / f"proc_{tag}.png"
                cv2.imwrite(str(p1), frame)
                cv2.imwrite(str(p2), proc)
                print(f"[按键] 已保存快照: {p1} , {p2}")
    except KeyboardInterrupt:
        print("\n[运行] 收到 Ctrl+C，退出")
    finally:
        if isinstance(scanner, qr.AsyncScanner):
            scanner.stop()
        if grabber is not None:
            grabber.stop()
        # 释放前抓一份最终状态（summary 里要用，释放后读不到）
        final = {"res": cam.resolution(), "fourcc": cam.fourcc(),
                 "mode": cam.mode.label() if cam.mode else "", "exp": cam.exposure_raw()}
        if csv_log:
            csv_log.close()
        if not args.keep_settings:
            try:
                cam.set_exposure_raw(orig["exposure"])
                if orig["gain"] is not None:
                    cam.set_gain(int(orig["gain"]))
                if orig["power_line"] is not None:
                    cam.ctl.set("power_line_frequency", int(orig["power_line"]))
                if orig["dyn_fps"] is not None:
                    cam.ctl.set("exposure_dynamic_framerate", int(orig["dyn_fps"]))
                cam.set_auto_exposure(bool(orig["auto_exposure"]))
                print(f"[退出] 已恢复相机原始设置 (auto_exposure="
                      f"{'AUTO' if orig['auto_exposure'] else 'MANUAL'}, "
                      f"raw {orig['exposure']})")
            except Exception as e:
                print(f"[退出] 恢复原始设置失败: {e}", file=sys.stderr)
        else:
            print("[退出] 已保留优化后的相机设置 (--keep-settings)")
        cam.release()
        if use_gui:
            try:
                cv2.destroyAllWindows()
                cv2.waitKey(1)
            except cv2.error:
                pass

    dur = max(time.time() - t_start, 1e-6)
    report.summary(meta, cam, frames, fails, dur, exp_min_seen, exp_max_seen,
                   final["exp"], clip_start, clip_end, scans, args.log_csv,
                   args.print_json, final=final, fps_proc=fps_proc,
                   extra={"roi": roi_label, "qr_enabled": scanner.enabled,
                          "dropped_frames": dropped, "qr_stats": scanner.stats})

    if switch_to_next:
        fresh = [devices.read_meta(n) for n in devices.capture_nodes()]
        nodes = [m.node for m in fresh] or [node]
        if len(nodes) < 2:
            print(f"[设备] 当前只有 1 个采集设备（{node}），无需切换。")
            return exit_code
        nxt = nodes[(nodes.index(node) + 1) % len(nodes)] if node in nodes else nodes[0]
        nm = next((m for m in fresh if m.node == nxt), fresh[0])
        devices.save_choice(nm)
        argv_new: List[str] = []
        skip = False
        for a in sys.argv[1:]:
            if skip:
                skip = False
                continue
            if a == "--device":
                skip = True
                continue
            if a.startswith("--device="):
                continue
            argv_new.append(a)
        argv_new += ["--device", nxt]
        print(f"[设备] 重新启动并使用 {nxt} ({nm.name}) …\n")
        sys.stdout.flush()
        try:
            os.execv(sys.executable, [sys.executable, "-m", "usbcam"] + argv_new)
        except Exception as e:
            print(f"[错误] 切换相机失败，请重新运行并指定 --device {nxt}: {e}", file=sys.stderr)
            return 5
    return exit_code


# ---------------------------------------------------------------------------
