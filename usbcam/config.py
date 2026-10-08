#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置记忆：默认相机、ROI、模式缓存。

跨机器通用：文件路径遵循 XDG，内容里不含任何本机写死的常量，
所有值都是运行时从设备读到的（序列号 / 物理安装 ID / 节点名 / 模式）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "usb_cam_monitor"


def config_path() -> Path:
    return config_home() / "config.json"


def load() -> Dict[str, Any]:
    """读配置；文件不存在/损坏都返回空 dict（不抛异常）。"""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(data: Dict[str, Any]) -> bool:
    try:
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def update(**fields: Any) -> bool:
    data = load()
    data.update(fields)
    return save(data)


def clear() -> bool:
    try:
        p = config_path()
        if p.exists():
            p.unlink()
        return True
    except Exception:
        return False


def get(key: str, default: Optional[Any] = None) -> Any:
    return load().get(key, default)
