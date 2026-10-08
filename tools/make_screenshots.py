#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 README 用的示意图（全部为合成画面，不含任何真实摄像头内容）。

    python3 tools/make_screenshots.py            # 写入 docs/

产出：
    docs/screenshot-hud.png    主窗口：右上角核心参数面板 + 左上角快捷键
    docs/screenshot-qr.png     扫码弹窗：左侧二维码图 + 右侧内容与设备信息
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from usbcam import overlay as ov, util  # noqa: E402
from usbcam import qr  # noqa: E402
from usbcam.stats import analyse  # noqa: E402

OUT = ROOT / "docs"


def fake_scene(w: int = 1280, h: int = 720) -> np.ndarray:
    """合成一张"书桌 + 过曝窗户 + 桌上二维码"的场景图（不含任何真实画面）。"""
    img = np.full((h, w, 3), (118, 116, 112), np.uint8)                  # 墙面（BGR）
    img = cv2.add(img, np.linspace(-18, 18, w, dtype=np.int16)[None, :, None]
                  .repeat(h, 0).repeat(3, 2).astype(np.uint8) * 0)
    cv2.rectangle(img, (0, int(h * 0.60)), (w, h), (96, 88, 78), -1)     # 桌面
    cv2.rectangle(img, (0, int(h * 0.60)), (w, int(h * 0.63)), (120, 112, 100), -1)
    # 过曝的窗户（用来演示 CLIP 指标）
    cv2.rectangle(img, (int(w * 0.70), int(h * 0.06)), (int(w * 0.97), int(h * 0.52)),
                  (252, 253, 255), -1)
    cv2.line(img, (int(w * 0.835), int(h * 0.06)), (int(w * 0.835), int(h * 0.52)),
             (215, 220, 228), 3)
    # 显示器
    cv2.rectangle(img, (int(w * 0.06), int(h * 0.16)), (int(w * 0.40), int(h * 0.58)),
                  (58, 56, 54), -1)
    cv2.rectangle(img, (int(w * 0.075), int(h * 0.18)), (int(w * 0.385), int(h * 0.55)),
                  (168, 172, 178), -1)
    cv2.rectangle(img, (int(w * 0.21), int(h * 0.58)), (int(w * 0.25), int(h * 0.63)),
                  (70, 68, 66), -1)
    # 桌上的杯子 + 笔记本
    cv2.circle(img, (int(w * 0.52), int(h * 0.70)), 46, (74, 96, 140), -1)
    cv2.circle(img, (int(w * 0.52), int(h * 0.70)), 34, (120, 150, 190), -1)
    cv2.rectangle(img, (int(w * 0.60), int(h * 0.70)), (int(w * 0.92), int(h * 0.94)),
                  (235, 238, 240), -1)
    # 桌上贴的二维码
    enc = cv2.QRCodeEncoder_create()
    code = enc.encode("https://example.com/unit/A-03")
    code = cv2.resize(code, (130, 130), interpolation=cv2.INTER_NEAREST)
    y0, x0 = int(h * 0.66), int(w * 0.62)
    img[y0 - 8:y0 + 138, x0 - 8:x0 + 138] = 250
    img[y0:y0 + 130, x0:x0 + 130] = cv2.cvtColor(code, cv2.COLOR_GRAY2BGR)
    img = cv2.GaussianBlur(img, (0, 0), 1.1)
    noise = np.random.normal(0, 2.5, img.shape)
    return np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)


class _FakeCam:
    """只为画 HUD 提供数值，不连真相机。"""

    def __init__(self) -> None:
        self.mode = type("M", (), {"label": lambda self_: "MJPG 1280x720@30"})()

    def resolution(self):
        return (1280, 720)

    def fourcc(self):
        return "MJPG"

    def gain(self):
        return None

    def declared_fps(self):
        return 30.0


class _FakeMeta:
    node = "/dev/video2"
    serial = "200901010001"
    install_id = "pci-0000:00:14.0-usb-0:1:1.0"

    def serial_or_na(self):
        return self.serial

    def install_or_na(self):
        return self.install_id


class _FakeCtrl:
    reason_en = "converged"
    last_reason = "已收敛"


class _FakeScanner:
    enabled = True
    last = None

    def status(self):
        return "扫 128/命中 1 深扫 6 精解 2 跟踪 9 6.4ms"

    def draw(self, img):
        return None


def make_hud(path: Path) -> None:
    from usbcam.enhance import Enhancer
    frame = fake_scene()
    enh = Enhancer(tone=True, knee=0.78, strength=0.6)
    proc = enh.apply(frame)
    st = analyse(proc, 250)
    cam = _FakeCam()
    lines = ov.hud_lines(_FakeMeta(), cam, 30.0, 194.8, proc, 80.0, st, _FakeCtrl(), enh,
                         True, False, "640x360+320+180", _FakeScanner(), False, True,
                         dropped=0, at_ceiling=True)
    ov.panel(proc, lines, align="right", scale=0.52)
    ov.help_panel(proc)
    # 模拟扫码命中：绿色定位框 + 底部提示条（与真实运行时一致）
    det = cv2.QRCodeDetector()
    text, pts, _ = det.detectAndDecode(proc)
    if text:
        cv2.polylines(proc, [pts.astype(int).reshape(-1, 1, 2)], True, (0, 255, 0), 2)
    label = f"QR: {text[:44]}" if text else "QR: (demo)"
    cv2.rectangle(proc, (0, proc.shape[0] - 34), (18 + 11 * len(label), proc.shape[0]),
                  (0, 0, 0), -1)
    cv2.putText(proc, label, (10, proc.shape[0] - 11), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                (0, 255, 0), 1, cv2.LINE_AA)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), proc)
    print(f"已生成 {path}")


def make_qr(path: Path) -> None:
    text = "工位A-03 设备点检通过 ✓\nhttps://example.com/usbcam?sn=200901010001"
    enc = cv2.QRCodeEncoder_create()
    qr_img = enc.encode(text)
    qr_big = cv2.resize(qr_img, (300, 300), interpolation=cv2.INTER_NEAREST)
    scene = fake_scene()
    scene[200:500, 460:760] = 255
    scene[210:510, 470:770][:300, :300] = cv2.cvtColor(qr_big, cv2.COLOR_GRAY2BGR)[:300, :300]
    det = cv2.QRCodeDetector()
    _t, pts, _ = det.detectAndDecode(scene)
    crop = qr.Scanner._crop(scene, pts)
    res = qr.ScanResult(text=text, kind="QR", points=pts, crop=crop,
                        stamp="2026-01-01 12:34:56.789 +08:00", stage="fast")
    popup = qr.compose_popup(res, [
        "Device: /dev/video2  Integrated Webcam",
        "SN: 200901010001",
        "ID: pci-0000:00:14.0-usb-0:1:1.0",
        "ROI: 640x360+320+180",
    ], font_path=util.find_cjk_font())
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), popup)
    print(f"已生成 {path}  ({popup.shape[1]}x{popup.shape[0]})")


if __name__ == "__main__":
    make_hud(OUT / "screenshot-hud.png")
    make_qr(OUT / "screenshot-qr.png")
