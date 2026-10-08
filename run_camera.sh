#!/usr/bin/env bash
# USB 相机过曝光优化 + 参数监视 + 扫码 —— 启动脚本
#
#   ./run_camera.sh                       自动选相机、开窗口、开启扫码
#   ./run_camera.sh --list                列出相机与支持模式
#   ./run_camera.sh --benchmark           实测各模式帧率 + ROI 开销
#   ./run_camera.sh --device serial:XXXX --set-default
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

EXTRA=()
if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    echo "[提示] 未检测到 DISPLAY，将使用无头模式（不弹窗）。" >&2
    EXTRA+=(--no-gui)
fi

exec python3 -m usbcam "${EXTRA[@]}" "$@"
