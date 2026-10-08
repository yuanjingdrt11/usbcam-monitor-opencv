#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫码鲁棒性测试台：用真实会遇到的退化条件（透视、模糊、低对比、反光、
小目标、噪声、JPEG 压缩、部分遮挡）评估识别率，并对比不同策略。

    python3 tests/test_qr_robustness.py            # 打印识别率表
    python3 tests/test_qr_robustness.py --save DIR # 同时保存测试图便于人工核对
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Callable, List, Tuple

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from usbcam.qr import Scanner  # noqa: E402

TEXT = "https://example.com/usbcam?sn=UNIT-42&ts=2026"


# ---------------------------------------------------------------------------
def make_qr(text: str = TEXT, modules: int = 0) -> np.ndarray:
    enc = cv2.QRCodeEncoder_create()
    qr = enc.encode(text)
    if qr is None:
        raise RuntimeError("QRCodeEncoder 不可用")
    return qr


def place_in_scene(qr: np.ndarray, frac: float = 0.25, size: Tuple[int, int] = (720, 1280),
                   bg: int = 110) -> np.ndarray:
    """把二维码按"占画面宽度 frac"贴到一张模拟场景图上。"""
    h, w = size
    side = max(24, int(w * frac))
    q = cv2.resize(qr, (side, side), interpolation=cv2.INTER_NEAREST)
    scene = np.full((h, w, 3), bg, np.uint8)
    scene[:, :, 1] = np.linspace(80, 150, w, dtype=np.uint8)[None, :]
    oy, ox = (h - side) // 2, (w - side) // 2
    scene[oy - 8:oy + side + 8, ox - 8:ox + side + 8] = 235
    scene[oy:oy + side, ox:ox + side] = cv2.cvtColor(q, cv2.COLOR_GRAY2BGR)
    return scene


def perspective(img: np.ndarray, deg: float) -> np.ndarray:
    h, w = img.shape[:2]
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    d = w * np.tan(np.deg2rad(deg)) * 0.5
    dst = np.float32([[d, 0], [w - d, d * 0.3], [w, h], [0, h - d * 0.3]])
    return cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (w, h),
                               borderValue=(110, 120, 110))


def rotate(img: np.ndarray, deg: float) -> np.ndarray:
    h, w = img.shape[:2]
    return cv2.warpAffine(img, cv2.getRotationMatrix2D((w / 2, h / 2), deg, 1.0), (w, h),
                          borderValue=(110, 120, 110))


def cases() -> List[Tuple[str, np.ndarray]]:
    qr = make_qr()
    base = place_in_scene(qr, 0.25)
    out: List[Tuple[str, np.ndarray]] = [
        ("基准 25% 画面", base),
        ("小目标 8% 画面", place_in_scene(qr, 0.08)),
        ("小目标 5% 画面", place_in_scene(qr, 0.05)),
        ("高斯模糊 sigma=2", cv2.GaussianBlur(base, (0, 0), 2.0)),
        ("高斯模糊 sigma=4", cv2.GaussianBlur(base, (0, 0), 4.0)),
        ("运动模糊 15px", cv2.blur(base, (15, 1))),
        ("低对比度 x0.30+95", cv2.convertScaleAbs(base, alpha=0.30, beta=95)),
        ("暗光 x0.35", cv2.convertScaleAbs(base, alpha=0.35, beta=0)),
        ("过曝 x1.6+40", cv2.convertScaleAbs(base, alpha=1.6, beta=40)),
        ("高斯噪声 sigma=25", cv2.add(base, np.random.normal(0, 25, base.shape).astype(np.int16)
                                     .clip(-255, 255).astype(np.uint8))),
        ("JPEG 质量20", cv2.imdecode(cv2.imencode(".jpg", base, [cv2.IMWRITE_JPEG_QUALITY, 20])[1],
                                     cv2.IMREAD_COLOR)),
        ("透视 18 度", perspective(base, 18)),
        ("透视 28 度", perspective(base, 28)),
        ("旋转 12 度", rotate(base, 12)),
        ("旋转 25 度", rotate(base, 25)),
        ("右下角 12%", corner_case(qr)),
        ("码上贴纸遮挡 12%", occluded(base)),
    ]
    return out


def corner_case(qr: np.ndarray) -> np.ndarray:
    h, w = 720, 1280
    side = int(w * 0.12)
    q = cv2.resize(qr, (side, side), interpolation=cv2.INTER_NEAREST)
    scene = np.full((h, w, 3), 110, np.uint8)
    scene[:, :, 2] = np.linspace(90, 160, w, dtype=np.uint8)[None, :]
    oy, ox = h - side - 30, w - side - 30
    scene[oy - 6:oy + side + 6, ox - 6:ox + side + 6] = 235
    scene[oy:oy + side, ox:ox + side] = cv2.cvtColor(q, cv2.COLOR_GRAY2BGR)
    return scene


def occluded(base: np.ndarray) -> np.ndarray:
    img = base.copy()
    h, w = img.shape[:2]
    side = int(w * 0.25)
    oy, ox = (h - side) // 2, (w - side) // 2
    img[oy + side - 26:oy + side, ox:ox + 26] = (128, 128, 128)      # 盖住一个角
    return img


# ---------------------------------------------------------------------------
def detect_once(scanner: Scanner, frame: np.ndarray, scans: int = 6) -> bool:
    """模拟连续扫描若干次（等价真实运行时约 0.6 秒），任一次解出即算成功。"""
    t = time.time()
    for i in range(scans):
        scanner._frame_idx = scanner.interval_frames - 1      # 去掉节流
        scanner._seen.clear()                                 # 去掉去重
        scanner._last_deep = 0.0                              # 每次都允许 deep
        res = scanner.scan(frame, now=t + i * 0.1)
        if res and res.text == TEXT:
            return True
    return False


def run(save_dir: Path | None = None) -> int:
    variants: List[Tuple[str, Callable[[], Scanner]]] = [
        ("旧策略: 单次480px", lambda: Scanner(scan_width=480, interval_frames=1, effort="fast",
                                            barcode=False, deep_every=1e9)),
        ("新策略: normal", lambda: Scanner(scan_width=480, interval_frames=1, effort="normal",
                                          barcode=False)),
        ("新策略: deep", lambda: Scanner(scan_width=480, interval_frames=1, effort="deep",
                                        barcode=False, deep_every=0.0)),
        ("新策略: deep+640px", lambda: Scanner(scan_width=640, interval_frames=1, effort="deep",
                                              barcode=False, deep_every=0.0)),
    ]
    data = cases()
    scanners = [(name, mk()) for name, mk in variants]

    print(f"{'测试场景':<22}" + "".join(f"{n:>20}" for n, _ in scanners))
    print("-" * (22 + 20 * len(scanners)))
    totals = [0] * len(scanners)
    for name, img in data:
        cells = []
        for i, (_n, sc) in enumerate(scanners):
            ok = detect_once(sc, img)
            totals[i] += 1 if ok else 0
            cells.append(f"{'OK' if ok else '未识别':>20}")
        print(f"{name:<22}" + "".join(cells))
        if save_dir:
            save_dir.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(save_dir / f"{name.replace(' ', '_').replace('%', 'pct')}.png"), img)
    print("-" * (22 + 20 * len(scanners)))
    n = len(data)
    print(f"{'识别率':<22}" + "".join(f"{t}/{n} ({t / n * 100:.0f}%)".rjust(20)
                                     for t in totals))
    # 新策略不应比旧策略差
    ok = totals[1] >= totals[0] and totals[2] >= totals[0]
    print("\n结论:", "新策略不劣于旧策略" + ("，且 deep 更强" if totals[2] > totals[0] else ""),
          "->", "通过" if ok else "未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", default=None, help="保存测试图的目录")
    a = ap.parse_args()
    sys.exit(run(Path(a.save) if a.save else None))
