#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设备发现、元数据与选择策略。

* 元数据（序列号 / 物理安装 ID / 拓扑 / VID:PID）全部来自 sysfs 与 v4l2-ctl，
  代码里不含任何本机标识，换机器/换相机同样工作。
* 物理安装 ID = 内核 by-path 给出的物理端口路径（去掉 -video-indexN），
  取不到时按 sysfs 拓扑（PCI 根 + USB 端口链 + 接口号）现算。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Tuple

from . import config, v4l2
from .util import now_stamp


# ---------------------------------------------------------------------------
# 设备元数据
# ---------------------------------------------------------------------------
@dataclass
class DeviceMeta:
    node: str = ""
    index: int = -1
    name: str = ""
    driver: str = ""
    bus_info: str = ""
    serial: str = ""
    install_id: str = ""
    usb_path: str = ""
    vid_pid: str = ""
    manufacturer: str = ""
    product: str = ""
    by_id: str = ""
    by_path: str = ""

    def serial_or_na(self) -> str:
        return self.serial if self.serial else "N/A (设备未提供序列号)"

    def install_or_na(self) -> str:
        return self.install_id if self.install_id else "N/A"

    def as_dict(self) -> dict:
        return asdict(self)


def video_nodes() -> List[str]:
    """按编号顺序返回 /dev/videoN（不假设具体编号存在）。"""
    found = []
    for p in Path("/dev").glob("video*"):
        m = re.fullmatch(r"video(\d+)", p.name)
        if m:
            found.append((int(m.group(1)), str(p)))
    return [n for _, n in sorted(found)]


def is_capture_node(node: str) -> bool:
    """是否是采集节点（排除 metadata 节点）。优先用 v4l2-ctl，其次实际试读一帧。"""
    if v4l2.have_v4l2ctl():
        text = v4l2.run(["v4l2-ctl", "-d", node, "--info"])
        if "Device Caps" in text:
            return "Video Capture" in text.split("Device Caps", 1)[1]
        return "Video Capture" in text
    import cv2
    cap = cv2.VideoCapture(node, cv2.CAP_V4L2)
    ok = cap.isOpened()
    got = False
    if ok:
        got, _ = cap.read()
    cap.release()
    return bool(got)


def capture_nodes() -> List[str]:
    return [n for n in video_nodes() if is_capture_node(n)]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""


def read_meta(node: str) -> DeviceMeta:
    """汇总一台设备的信息（全部运行时查询，无写死值）。"""
    meta = DeviceMeta(node=node)
    m = re.fullmatch(r"/dev/video(\d+)", node)
    meta.index = int(m.group(1)) if m else -1
    sysdir = Path(f"/sys/class/video4linux/video{meta.index}")
    iface = sysdir / "device"
    try:
        iface_real = iface.resolve()
    except Exception:
        iface_real = iface
    usb_dev = iface_real.parent

    meta.name = _read(sysdir / "name")
    meta.serial = _read(usb_dev / "serial")
    meta.manufacturer = _read(usb_dev / "manufacturer")
    meta.product = _read(usb_dev / "product")
    vid, pid = _read(usb_dev / "idVendor"), _read(usb_dev / "idProduct")
    if vid and pid:
        meta.vid_pid = f"{vid}:{pid}"
    busnum, devpath = _read(usb_dev / "busnum"), _read(usb_dev / "devpath")
    if busnum and devpath:
        meta.bus_info = f"usb-{busnum}-{devpath}"
    if ":" in iface_real.name:
        meta.usb_path = f"{usb_dev.name}:{iface_real.name.split(':', 1)[1]}"
    else:
        meta.usb_path = usb_dev.name

    # 稳定符号链接（by-id / by-path）
    try:
        target = str(Path(node).resolve())
        for p in sorted(Path("/dev/v4l/by-id").glob("*")):
            if str(p.resolve()) == target and p.name.endswith("index0"):
                meta.by_id = p.name
                break
        for p in sorted(Path("/dev/v4l/by-path").glob("*")):
            if str(p.resolve()) == target and p.name.endswith("index0"):
                meta.by_path = p.name
                break
    except Exception:
        pass

    # 物理安装 ID：优先 by-path（内核给的物理端口路径）
    if meta.by_path:
        meta.install_id = re.sub(r"-video-index\d+$", "", meta.by_path)
    else:
        root = _pci_root(iface_real)
        if root:
            port = usb_dev.name.split("-", 1)[-1] if "-" in usb_dev.name else usb_dev.name
            cfg = iface_real.name.split(":", 1)[1] if ":" in iface_real.name else "1.0"
            meta.install_id = f"pci-{root}-usb-0:{port}:{cfg}"

    # 驱动 / 总线信息以 v4l2-ctl 为准
    if v4l2.have_v4l2ctl():
        text = v4l2.run(["v4l2-ctl", "-d", node, "--info"])
        mb = re.search(r"^\s*Bus info\s*:\s*(\S+)\s*$", text, re.M)
        if mb:
            meta.bus_info = mb.group(1)
        md = re.search(r"^\s*Driver name\s*:\s*(\S+)\s*$", text, re.M)
        if md:
            meta.driver = md.group(1)
        if not meta.serial:
            ms = re.search(r"^\s*Serial\s*:\s*(\S+)\s*$", text, re.M)
            if ms:
                meta.serial = ms.group(1)

    # 兜底：by-id 名称末尾常带序列号（要求同时含字母和数字，避免把型号当序列号）
    if not meta.serial and meta.by_id:
        body = re.sub(r"-video-index\d+$", "", re.sub(r"^usb-", "", meta.by_id))
        parts = body.split("_")
        if len(parts) >= 3:
            cand = parts[-1]
            if (len(cand) >= 6 and re.fullmatch(r"[0-9A-Za-z]+", cand)
                    and re.search(r"\d", cand) and re.search(r"[A-Za-z]", cand)):
                meta.serial = cand
    return meta


def _pci_root(path: Path) -> str:
    for parent in [path] + list(path.parents):
        if re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.\d", parent.name):
            return parent.name
    return ""


# ---------------------------------------------------------------------------
# 选择与记忆
# ---------------------------------------------------------------------------
def match_saved(saved: dict, metas: List[DeviceMeta]) -> Optional[DeviceMeta]:
    """按 序列号 -> 物理安装 ID -> 节点名 匹配记住的相机。"""
    if not saved:
        return None
    for key, attr in (("serial", "serial"), ("install_id", "install_id"), ("node", "node")):
        want = (saved.get(key) or "").strip()
        if not want:
            continue
        for m in metas:
            if getattr(m, attr) == want:
                return m
    return None


def save_choice(meta: DeviceMeta, quiet: bool = False) -> bool:
    cur = config.load()
    same = (cur.get("serial") == meta.serial and cur.get("install_id") == meta.install_id
            and cur.get("node") == meta.node)
    ok = config.update(serial=meta.serial, install_id=meta.install_id, node=meta.node,
                       name=meta.name, vid_pid=meta.vid_pid, saved_at=now_stamp())
    if ok and not quiet and not same:
        print(f"[设备] 已记住默认相机: {meta.node}  {meta.name}  "
              f"(SN={meta.serial_or_na()}, 安装ID={meta.install_or_na()})")
    return ok


def describe(meta: DeviceMeta) -> str:
    return (f"{meta.node}  {meta.name}\n"
            f"        SN={meta.serial_or_na()}   安装ID={meta.install_or_na()}   "
            f"VID:PID={meta.vid_pid or 'N/A'}")


def _ask(metas: List[DeviceMeta]) -> Optional[DeviceMeta]:
    print("检测到多个采集设备，请选择要使用的相机：")
    for i, m in enumerate(metas, 1):
        print(f"  [{i}] {describe(m)}")
    print("  输入序号后回车（直接回车 = 1，输入 q 退出）")
    while True:
        try:
            ans = input("选择相机> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if ans == "":
            return metas[0]
        if ans in ("q", "quit", "exit"):
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(metas):
            return metas[int(ans) - 1]
        print("  输入无效，请输入列表中的序号")


def choose(spec: str, metas: List[DeviceMeta],
           allow_ask: bool = True) -> Tuple[Optional[DeviceMeta], str]:
    """解析 --device，返回 (设备, 选择来源)。

    支持 auto | 索引 | /dev/videoN | serial:XXX | name:XXX | install:XXX。
    auto 顺序：记住的相机 -> 唯一设备 -> 终端询问 -> 回退第一个。
    （不再用"节点 mtime 最新"猜设备：USB 重新枚举时 mtime 会互换，会选错。）
    """
    if not metas:
        return None, "无可用采集设备"
    spec = (spec or "auto").strip()

    if spec != "auto":
        if spec.startswith("/dev/"):
            for m in metas:
                if m.node == spec:
                    return m, "命令行 --device"
            if os.path.exists(spec):
                return None, f"{spec} 不是视频采集节点（metadata 节点不能采集）"
            return None, f"找不到设备 {spec}"
        if spec.isdigit():
            cand = f"/dev/video{spec}"
            for m in metas:
                if m.node == cand:
                    return m, "命令行 --device"
            return None, f"找不到采集设备 {cand}"
        if ":" in spec:
            key, val = spec.split(":", 1)
            getter = {"serial": lambda m: m.serial, "name": lambda m: m.name,
                      "install": lambda m: m.install_id, "id": lambda m: m.install_id,
                      "node": lambda m: m.node}.get(key.strip().lower())
            val = val.strip()
            if getter is None:
                return None, f"不支持的 --device 写法: {spec}（可用 serial:/name:/install:）"
            for m in metas:
                if val and val.lower() in (getter(m) or "").lower():
                    return m, f"命令行 --device {key}:"
            return None, f"没有匹配 {spec} 的相机"

    saved = config.load()
    m = match_saved(saved, metas)
    if m is not None:
        return m, "已记住的默认相机"
    if saved and (saved.get("serial") or saved.get("install_id") or saved.get("node")):
        print(f"[警告] 记住的默认相机当前不在线：SN={saved.get('serial') or '无'} "
              f"安装ID={saved.get('install_id') or '无'} 上次节点={saved.get('node') or '无'}。"
              f"请检查相机是否插好（--list 查看当前设备）。", file=sys.stderr)
    if len(metas) == 1:
        return metas[0], "唯一可用相机"
    if allow_ask and sys.stdin.isatty():
        m = _ask(metas)
        return (m, "交互选择") if m else (None, "用户取消选择")
    return metas[0], "非交互环境回退到第一个（可用 --device 指定）"


def list_report() -> int:
    nodes = capture_nodes()
    print("=" * 78)
    print("可用视频设备 (Video Capture)")
    print("=" * 78)
    if not nodes:
        print("  未发现采集设备")
        return 1
    metas = [read_meta(n) for n in nodes]
    remembered = match_saved(config.load(), metas)
    for m in metas:
        mark = "   <== 默认(已记住)" if (remembered and m.node == remembered.node) else ""
        print(f"  {describe(m)}{mark}")
        print(f"        by-id: {m.by_id or 'N/A'}")
        modes = v4l2.list_modes(m.node)
        if modes:
            fastest = max(modes, key=lambda x: (x.fps, x.pixels))
            print(f"        支持模式: {len(modes)} 种，最高声明帧率 "
                  f"{fastest.fps:g}fps ({fastest.width}x{fastest.height} {fastest.fourcc})")
    print("-" * 78)
    print(f"  默认相机: {remembered.node + ' ' + remembered.name if remembered else '尚未记住'}")
    print("  指定方式: --device /dev/videoN | --device N | --device serial:XXX |"
          " --device name:XXX | --device install:XXX")
    print("  子命令  : --set-default 记住某台相机后退出；--forget 清除记忆")
    return 0
