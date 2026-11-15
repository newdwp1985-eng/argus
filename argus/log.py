"""操作日志：所有工具调用与动作均落盘，便于追溯。"""

from __future__ import annotations

import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler

# 日志目录可在安装后用环境变量改到用户目录，避免装成只读包后写不进去。
LOG_DIR = os.environ.get("ARGUS_LOG_DIR") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "logs")
)
os.makedirs(LOG_DIR, exist_ok=True)

_ACTION_LOG = os.path.join(LOG_DIR, "actions.jsonl")
_ACTION_MAX_BYTES = 4_000_000
_ACTION_KEEP = 3


def _rotate_action_log() -> None:
    """审计日志轮转：超过上限就顺延，避免长期运行无限增长。"""
    try:
        if os.path.getsize(_ACTION_LOG) < _ACTION_MAX_BYTES:
            return
    except OSError:
        return
    for i in range(_ACTION_KEEP - 1, 0, -1):
        src = f"{_ACTION_LOG}.{i}"
        if os.path.exists(src):
            os.replace(src, f"{_ACTION_LOG}.{i + 1}")
    try:
        os.replace(_ACTION_LOG, f"{_ACTION_LOG}.1")
    except OSError:
        pass


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")

    fh = RotatingFileHandler(
        os.path.join(LOG_DIR, "argus.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.propagate = False
    return logger


def log_action(operation_id: str, action: str, **fields) -> None:
    """追加一条结构化动作记录（JSONL）。"""
    record = {"ts": time.time(), "operation_id": operation_id, "action": action}
    record.update(fields)
    _rotate_action_log()
    try:
        with open(_ACTION_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:  # 日志失败不应影响主流程
        logging.getLogger("argus.log").warning("写动作日志失败: %s", exc)
