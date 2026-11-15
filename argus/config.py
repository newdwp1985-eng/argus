"""配置加载：把散落在代码里的实测阈值收进一份可替换的 JSON。

设计取舍：坐标与比例不是「魔法数字」，而是「在特定窗口尺寸、特定字号下实测
出来的边界」。写死在源码里意味着换显示器、改微信字号、拉大窗口都要改代码；
放进 JSON 则可以换档而不动逻辑。

优先级：环境变量 ``ARGUS_CONFIG`` 指定的文件 > 包内 ``profile_default.json``。

文件改动会在下次读取时自动生效（按 mtime 判断），不必重启进程。
"""

from __future__ import annotations

import json
import os
import threading

_PKG = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(_PKG, "profile_default.json")
ENV_KEY = "ARGUS_CONFIG"

_lock = threading.Lock()
_cache: dict = {"data": None, "mtime": -1.0, "path": ""}


def _resolve_path() -> str:
    return os.environ.get(ENV_KEY) or DEFAULT_PATH


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        # 配置坏了不能拖垮业务：退回空字典，所有读取都会落到默认值
        return {}


def load(force: bool = False) -> dict:
    """读取配置。文件变了就重载，没变就复用。"""
    path = _resolve_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = -1.0
    with _lock:
        stale = force or _cache["data"] is None or _cache["path"] != path
        changed = mtime != _cache["mtime"]
        if stale or changed:
            _cache["data"] = _read(path)
            _cache["mtime"] = mtime
            _cache["path"] = path
        return _cache["data"]


def cfg(dotted: str, default=None):
    """按 ``a.b.c`` 取配置值，缺失时返回 default。"""
    node = load()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node if node is not None else default


def cfg_float(dotted: str, default: float) -> float:
    try:
        return float(cfg(dotted, default))
    except (TypeError, ValueError):
        return default


def cfg_int(dotted: str, default: int) -> int:
    try:
        return int(cfg(dotted, default))
    except (TypeError, ValueError):
        return default


def describe() -> dict:
    """当前生效的配置来源，供 status 工具与排障使用。"""
    path = _resolve_path()
    return {
        "path": path,
        "exists": os.path.exists(path),
        "from_env": bool(os.environ.get(ENV_KEY)),
    }
