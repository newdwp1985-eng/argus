"""全局互斥。

为什么需要：一次发送的链路是「切会话 → 点输入框 → 粘贴 → 点发送 → 复核」。
若两次调用在时间上重叠，第二次切会话会打断第一次，结果就是把 A 的内容发进 B
的窗口 —— 这是本系统最不能犯的错，必须靠结构排除，而不是靠调用方自觉。

为什么串行而不是细粒度锁：屏幕、鼠标、剪贴板、前台窗口都只有一份，物理上
并发不了。排队只会变慢，不会出错；并发才可能出错。

锁是可重入的（RLock）：``get_chat_history`` 内部还要调 ``_open_contact``，
两者都加锁时不会自己把自己锁死。
"""

from __future__ import annotations

import functools
import threading
import time

from .errors import EnvUnavailable, Err
from .log import get_logger

logger = get_logger("argus.lock")

_GUARD = threading.RLock()

# 仅用于出错时给出人话提示，读写靠 GIL 保证原子性，不参与同步。
_state: dict = {"owner": None, "since": 0.0}


def current() -> dict:
    """当前持锁者快照（无锁时为 None）。"""
    return dict(_state)


def _mark(owner: str) -> None:
    _state["owner"] = owner
    _state["since"] = time.time()


def _clear() -> None:
    _state["owner"] = None
    _state["since"] = 0.0


def _busy_detail() -> str:
    snap = current()
    if not snap["owner"]:
        return "另一项操作正在进行"
    waited = time.time() - snap["since"]
    return f"另一项操作「{snap['owner']}」正在进行（已持续 {waited:.1f}s）"


def try_acquire(timeout: float) -> bool:
    """尝试取锁，超时返回 False 而不是无限等待。"""
    if timeout is None or timeout <= 0:
        _GUARD.acquire()
        return True
    return _GUARD.acquire(timeout=timeout)


def release() -> None:
    try:
        _GUARD.release()
    except RuntimeError:  # 未持有时释放，不应影响主流程
        logger.warning("释放未持有的锁，已忽略")
    if not _GUARD._is_owned():  # noqa: SLF001 仅用于观测状态
        _clear()


def serialized(timeout: float | None = None):
    """给工具函数加串行闸门。

    超时后不抛异常，而是返回结构化的 ``BUSY``，让调用方明确知道「没做」，
    而不是让它以为「做失败了」。这个区别对上层重试决策很关键。
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            from .result import fail, new_operation_id

            limit = timeout
            if limit is None:
                try:
                    from .config import cfg

                    limit = float(cfg("limits.lock_timeout_seconds", 30.0))
                except Exception:  # noqa: BLE001 配置不可用不应阻断业务
                    limit = 30.0

            got = try_acquire(limit)
            if not got:
                detail = _busy_detail()
                logger.warning("并发调用被拒绝: %s", detail)
                return fail(f"{detail}，本次调用未执行", Err.BUSY.value, new_operation_id())
            _mark(fn.__name__)
            try:
                return fn(*args, **kwargs)
            except EnvUnavailable as exc:
                # 环境不满足（非 macOS、缺 pyobjc/Pillow/numpy 等）：这不是业务失败，
                # 但也不能让 traceback 冒到调用方。统一转成结构化错误码返回，
                # 让 Agent 能读出「为什么没做」而不是看到一段堆栈。
                logger.warning("环境不满足，未执行 %s: %s", fn.__name__, exc)
                return fail(str(exc), exc.error_code, new_operation_id())
            finally:
                release()
                _clear()

        return wrapper

    return decorator
