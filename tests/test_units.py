#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元测试（不依赖真实相机）。

    python3 tests/test_units.py        # 直接跑
    pytest tests/test_units.py         # 用 pytest 跑
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="usbcam_cfg_"))

from usbcam import config, util  # noqa: E402
from usbcam.capture import choose_mode  # noqa: E402
from usbcam.enhance import Enhancer, build_tone_lut  # noqa: E402
from usbcam.exposure import ExposureController  # noqa: E402
from usbcam.qr import Scanner  # noqa: E402
from usbcam.roi import Roi  # noqa: E402
from usbcam.stats import analyse  # noqa: E402
from usbcam.v4l2 import Mode, parse_modes  # noqa: E402


# ---------------------------------------------------------------------------
def test_parse_modes():
    sample = """
ioctl: VIDIOC_ENUM_FMT
\tType: Video Capture

\t[0]: 'MJPG' (Motion-JPEG, compressed)
\t\tSize: Discrete 1280x720
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\t\tInterval: Discrete 0.067s (15.000 fps)
\t\tSize: Discrete 640x480
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t[1]: 'YUYV' (YUYV 4:2:2)
\t\tSize: Discrete 640x480
\t\t\tInterval: Discrete 0.100s (10.000 fps)
"""
    modes = parse_modes(sample)
    assert Mode("MJPG", 1280, 720, 30.0) in modes
    assert Mode("MJPG", 640, 480, 30.0) in modes
    assert Mode("YUYV", 640, 480, 10.0) in modes
    assert len(modes) == 3, modes


def test_choose_mode_policies():
    modes = [Mode("MJPG", 1920, 1080, 30), Mode("MJPG", 1280, 720, 30),
             Mode("MJPG", 640, 480, 60), Mode("YUYV", 640, 480, 30),
             Mode("YUYV", 320, 240, 30)]
    # 默认：先取最高帧率档（60fps 只有 640x480）
    assert choose_mode(modes).label() == "MJPG 640x480@60"
    # 抬高像素上限也仍然优先帧率
    assert choose_mode(modes, max_pixels=1920 * 1080).label() == "MJPG 640x480@60"
    # 限定尺寸 -> 该尺寸里帧率最高
    assert choose_mode(modes, size=(640, 480)).label() == "MJPG 640x480@60"
    # 限定帧率上限 30 -> 30fps 档里最大画面且不超过 max_pixels
    assert choose_mode(modes, fps=30).label() == "MJPG 1280x720@30"
    # 同一帧率档里若 MJPG 没有满足像素上限的，就让位给满足上限的 YUYV（像素上限优先于格式偏好）
    assert choose_mode(modes, fps=30, max_pixels=640 * 480).label() == "YUYV 640x480@30"
    # 指定格式：YUYV 里 30fps 档中不超过 max_pixels 的最大画面
    assert choose_mode(modes, fourcc="YUYV").label() == "YUYV 640x480@30"
    assert choose_mode(modes, fourcc="YUYV", max_pixels=320 * 240).label() == "YUYV 320x240@30"
    # 设备不支持的尺寸 -> 退到面积最接近的，不报错
    assert choose_mode(modes, size=(1000, 1000)) is not None
    # 空列表
    assert choose_mode([]) is None


def test_roi_parse_and_apply():
    img = np.zeros((100, 200, 3), np.uint8)
    img[10:30, 20:60] = 255
    r = Roi.parse("20,10,40,20", 200, 100)
    assert (r.x, r.y, r.w, r.h) == (20, 10, 40, 20)
    assert r.apply(img).shape[:2] == (20, 40)
    assert int(r.apply(img).mean()) == 255
    # 百分比
    r2 = Roi.parse("10,10,20,20%", 200, 100)
    assert (r2.x, r2.y, r2.w, r2.h) == (20, 10, 40, 20)
    # 越界要被夹回画面内
    r3 = Roi.parse("190,90,100,100", 200, 100)
    assert r3.x + r3.w <= 200 and r3.y + r3.h <= 100
    # 非法输入
    assert Roi.parse("bad", 200, 100) is None
    assert Roi.parse("1,2,3", 200, 100) is None
    # 整幅判定
    assert Roi.full(200, 100).is_full(200, 100)
    assert not r.is_full(200, 100)


def test_roi_config_roundtrip():
    r = Roi(10, 20, 30, 40)
    old = config.load()
    try:
        from usbcam import roi as roi_mod
        roi_mod.save(r, 200, 100)
        got = roi_mod.load(200, 100)
        assert got is not None
        assert abs(got.x - 10) <= 1 and abs(got.y - 20) <= 1
        assert abs(got.w - 30) <= 1 and abs(got.h - 40) <= 1
        # 换分辨率后按比例缩放
        got2 = roi_mod.load(400, 200)
        assert abs(got2.w - 60) <= 2 and abs(got2.h - 80) <= 2
    finally:
        config.save(old)


def test_tone_lut_properties():
    lut = build_tone_lut(knee=0.78, strength=0.6)
    idx = np.arange(256)
    assert np.all(np.diff(lut.astype(int)) >= 0), "LUT 必须单调"
    assert np.all(lut <= idx), "高光只能压暗，不能提亮"
    assert lut[255] == 255 and lut[0] == 0
    assert np.array_equal(lut[:190], idx[:190]), "knee 以下不应改动"
    assert lut[240] < 240, "高光应被压缩"
    # gamma 变体仍然单调
    lut2 = build_tone_lut(knee=0.7, strength=0.9, gamma=0.9)
    assert np.all(np.diff(lut2.astype(int)) >= 0)


def test_enhancer_reduces_highlights():
    img = np.full((40, 40, 3), 255, np.uint8)
    img[:20] = 200
    out = Enhancer(tone=True).apply(img)
    assert out[0, 0, 2] <= 200          # 200 被压暗
    assert out[30, 0, 2] == 255         # 纯白仍是纯白
    # 关闭时原样返回
    same = Enhancer(tone=False, clahe=False).apply(img)
    assert same is img


def test_stats_analyse():
    img = np.zeros((100, 100, 3), np.uint8)
    img[:10] = 255                       # 10% 死白
    img[10:20] = 252                     # 10% 接近溢出
    st = analyse(img, clip_threshold=250)
    assert abs(st.sat_pct - 10.0) < 0.6, st
    assert abs(st.clip_pct - 20.0) < 0.6, st
    assert st.p999 >= 250


def test_exposure_controller_converges_without_oscillation():
    c = ExposureController(50, 316, interval=0.0)

    class S:
        pass

    def frame_at(raw):
        vd = 255 * (raw / 320) * 0.35
        vb = 255 * (raw / 320) * 1.40
        s = S()
        s.luma = 0.8 * min(vd, 255) + 0.2 * min(vb, 255)
        s.clip_pct = 20.0 if vb >= 250 else 0.0
        s.sat_pct = 20.0 if vb >= 255 else 0.0
        s.p999 = min(255.0, vb)
        return s

    raw, t, hist = 320.0, 0.0, []
    for _ in range(400):
        st = frame_at(raw)
        new = c.update(raw, st, t)
        t += 1 / 30
        if new is not None:
            raw = float(new)
        hist.append(raw)
    tail = hist[-90:]
    assert max(tail) - min(tail) <= 2, f"末 3 秒曝光应稳定，实际波动 {max(tail) - min(tail)}"
    assert frame_at(raw).clip_pct < 1.2, "收敛后不应还有明显溢出"
    assert raw > 50, "不应被压到最低曝光"


def test_exposure_controller_limits():
    c = ExposureController(50, 10000, interval=0.0)

    class S:
        pass

    s = S(); s.clip_pct = 0.0; s.sat_pct = 0.0; s.luma = 10.0; s.p999 = 20.0
    raw = 50.0
    for i in range(200):
        new = c.update(raw, s, i * 0.1)
        if new is not None:
            raw = float(new)
        if c.limit_state == "max":
            break
    assert c.limit_state == "max" and "最大曝光" in c.advice()

    c2 = ExposureController(50, 10000, interval=0.0)
    s2 = S(); s2.clip_pct = 40.0; s2.sat_pct = 30.0; s2.luma = 30.0; s2.p999 = 255.0
    out = c2.update(200.0, s2, 0.0)
    assert out is None and c2.limit_state == "hdr", "逆光时应停止降曝光"


def test_util_helpers():
    assert util.ascii_safe("中文abc") == "abc"
    assert util.ascii_safe("x" * 100, 5) == "xxxxx"
    assert util.has_cjk("中文") and not util.has_cjk("abc")
    lines = util.wrap_text("中" * 10, 10)
    assert len(lines) >= 2 and all(l for l in lines)
    assert util.RAW_PER_MS == 10.0


def test_config_roundtrip():
    old = config.load()
    try:
        assert config.save({"serial": "X1", "node": "/dev/videoX"})
        assert config.get("serial") == "X1"
        assert config.load()["node"] == "/dev/videoX"
    finally:
        config.save(old)


def _qr_frame(text: str, size: int = 720) -> np.ndarray:
    frame = np.full((size, size, 3), 128, np.uint8)
    enc = cv2.QRCodeEncoder_create()
    qr = enc.encode(text)
    q = cv2.resize(qr, (size // 2, size // 2), interpolation=cv2.INTER_NEAREST)
    o = (size - q.shape[0]) // 2
    frame[o:o + q.shape[0], o:o + q.shape[1]] = cv2.cvtColor(q, cv2.COLOR_GRAY2BGR)
    return frame


def test_scanner_detects_and_dedupes():
    text = "https://example.com/x?sn=UNIT1"
    frame = _qr_frame(text)
    sc = Scanner(enabled=True, interval_frames=1, cooldown=5.0)
    first = sc.scan(frame, now=1000.0)
    assert first is not None and first.text == text, "应识别出二维码"
    assert first.crop is not None and min(first.crop.shape[:2]) > 10
    again = sc.scan(frame, now=1001.0)
    assert again is None, "冷却期内同内容不应重复提示"
    later = sc.scan(frame, now=1010.0)
    assert later is not None, "冷却期过后应再次提示"
    assert sc.last_cost_ms >= 0


def test_scanner_disabled_and_empty():
    sc = Scanner(enabled=False)
    assert sc.scan(np.zeros((64, 64, 3), np.uint8)) is None
    sc2 = Scanner(enabled=True, interval_frames=1)
    assert sc2.scan(np.zeros((0, 0, 3), np.uint8)) is None


def test_scanner_no_false_positive():
    sc = Scanner(enabled=True, interval_frames=1)
    assert sc.scan(np.full((480, 640, 3), 127, np.uint8), now=1.0) is None


# ---------------------------------------------------------------------------
def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  [OK]   {name}")
        except AssertionError as e:
            failed.append((name, e))
            print(f"  [FAIL] {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed.append((name, e))
            print(f"  [ERROR] {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
