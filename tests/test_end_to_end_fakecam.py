#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端测试：用"假相机"（合成含二维码的画面）跑完整的 app 主循环。

不依赖真实硬件，可在 CI/无相机环境执行：
    python3 tests/test_end_to_end_fakecam.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def make_qr_frame(width: int = 1280, height: int = 720, text: str = "") -> np.ndarray:
    """合成一帧：白底 + 居中二维码 + 一点渐变，模拟真实场景。"""
    frame = np.full((height, width, 3), 90, np.uint8)
    grad = np.linspace(40, 160, width, dtype=np.uint8)
    frame[:, :, 1] = grad[None, :]
    text = text or "https://example.com/usbcam?sn=TEST123&t=2026"
    enc = cv2.QRCodeEncoder_create()
    qr = enc.encode(text)
    if qr is None:
        raise RuntimeError("QRCodeEncoder 不可用")
    side = min(height // 2, width // 3)
    qr = cv2.resize(qr, (side, side), interpolation=cv2.INTER_NEAREST)
    x0 = (width - side) // 2
    y0 = (height - side) // 2
    pad = 12
    frame[y0 - pad:y0 + side + pad, x0 - pad:x0 + side + pad] = 255
    frame[y0:y0 + side, x0:x0 + side] = cv2.cvtColor(qr, cv2.COLOR_GRAY2BGR)
    return frame


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="usbcam_test_"))
    cfg = tmp / "cfg"
    cfg.mkdir(parents=True, exist_ok=True)
    os.environ["XDG_CONFIG_HOME"] = str(cfg)

    import usbcam.capture as capture
    from usbcam import app as app_mod
    from usbcam.devices import DeviceMeta

    qr_text = "https://example.com/usbcam?sn=TEST123&t=2026"
    frames_served = {"n": 0}

    class FakeCamera:
        """满足 app 用到的接口：模式来自"设备支持列表"，读帧返回合成二维码画面。"""

        def __init__(self, node, fourcc="auto", size=None, fps=None, buffers=3,
                     max_pixels=1280 * 720, use_v4l2ctl=True, auto_probe=True):
            self.node = node
            self.buffers = buffers
            from usbcam.v4l2 import Mode
            self.modes = [Mode("MJPG", 1280, 720, 30.0), Mode("MJPG", 640, 480, 60.0)]
            self.mode = self.modes[0]
            self.measured_fps = 30.0
            self.exp_min, self.exp_max = 50, 10000
            self.has_gain = False
            self._exp = 100.0
            self.frame = make_qr_frame(1280, 720, qr_text)
            self._first = True

            class _Ctl:
                available = False

                def has(self, *a):
                    return False

                def get(self, *a):
                    return None

                def set(self, *a):
                    return False

                def limit(self, *a, **k):
                    return k.get("default", 0)

            self.ctl = _Ctl()

        # --- app 用到的接口 ---
        def open(self):
            return True

        def release(self):
            pass

        def read(self):
            frames_served["n"] += 1
            img = self.frame.copy()
            if frames_served["n"] > 30:
                img = cv2.convertScaleAbs(img, alpha=1.6, beta=20)   # 后段模拟过曝
            return True, img

        def resolution(self):
            return (1280, 720)

        def fourcc(self):
            return "MJPG"

        def declared_fps(self):
            return 30.0

        def exposure_raw(self):
            return self._exp

        def set_exposure_raw(self, v):
            self._exp = float(v)
            return self._exp

        def set_auto_exposure(self, on):
            return True

        def auto_exposure_enabled(self):
            return False

        def gain(self):
            return None

        def set_gain(self, v):
            return False

        def set_auto_wb(self, on):
            return False

        def max_exposure_raw(self, ratio=0.95):
            return 316

    # --- 打桩：设备发现 + 相机类 ---
    meta = DeviceMeta(node="/dev/fake0", name="FakeCam (test)", serial="TEST123",
                      install_id="pci-test-usb-0:0:1.0", vid_pid="0000:0000")
    capture.Camera = FakeCamera
    app_mod.Camera = FakeCamera
    app_mod.devices.capture_nodes = lambda: ["/dev/fake0"]
    app_mod.devices.read_meta = lambda node: meta

    scans_dir = tmp / "scans"
    snaps_dir = tmp / "snapshots"
    code = app_mod.main([
        "--no-gui", "--duration", "3", "--status-interval", "90",
        "--qr-dir", str(scans_dir), "--snapshot-dir", str(snaps_dir),
        "--width" if False else "--max-pixels", "921600",
        "--qr-popup", "off",
    ])

    # --- 断言 ---
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  [OK]   " if cond else "  [FAIL] ") + msg)
        ok = ok and bool(cond)

    print("\n=== 假相机端到端测试 ===")
    check(code == 0, f"进程退出码 == 0 (实际 {code})")
    check(frames_served["n"] > 30, f"确实读了多帧 (实际 {frames_served['n']})")
    jsonl = scans_dir / "scans.jsonl"
    check(jsonl.exists(), f"生成了扫码记录 {jsonl}")
    texts = []
    if jsonl.exists():
        import json
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                texts.append(json.loads(line))
    check(any(r.get("text") == qr_text for r in texts),
          f"扫码内容正确 ({len(texts)} 条记录)")
    check(any(r.get("serial") == "TEST123" for r in texts), "记录里带了设备序列号")
    crops = list(scans_dir.glob("qr_*.png"))
    check(bool(crops), f"保存了二维码裁剪图 ({len(crops)} 个)")
    if crops:
        img = cv2.imread(str(crops[0]))
        check(img is not None and min(img.shape[:2]) >= 16,
              f"裁剪图可读且尺寸合理 {None if img is None else img.shape}")

    # --- 弹窗合成测试 ---
    print("\n=== 扫码弹窗合成 ===")
    from usbcam import qr as qr_mod
    res = qr_mod.ScanResult(text="中文测试：扫码内容 ✓\nhttps://example.com",
                            kind="QR", points=np.zeros((4, 2), np.float32),
                            crop=make_qr_frame(400, 400)[50:350, 50:350],
                            stamp="2026-01-01 00:00:00.000 +08:00")
    popup = qr_mod.compose_popup(res, ["Device: /dev/fake0", "SN: TEST123"],
                                 font_path="/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    check(popup is not None and popup.size > 0, f"弹窗图片生成成功 {popup.shape}")
    out = tmp / "popup_preview.png"
    cv2.imwrite(str(out), popup)
    print(f"  弹窗预览已保存: {out}")

    print(f"\n临时目录: {tmp}")
    if ok:
        shutil.rmtree(tmp, ignore_errors=True)
    print("=== 结果:", "全部通过 ===" if ok else "存在失败 ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
