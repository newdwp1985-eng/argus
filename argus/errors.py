"""错误码收口。

原先十几个错误码以字符串字面量散落在各工具内部，上游按码分支时只要拼错一个
字母就会静默失效，且没有任何地方能列出「一共可能出现哪些错误」。这里统一成
枚举，并给引擎状态一个显式映射表：映射表里没有的状态一律归为 TOOL_ERROR，
不再靠 `.upper()` 猜。
"""

from __future__ import annotations

from enum import Enum


class Err(str, Enum):
    """全部对外错误码。新增错误必须加在这里，禁止再写字面量。"""

    # --- 环境与界面 ---
    WECHAT_NOT_RUNNING = "WECHAT_NOT_RUNNING"
    WINDOW_NOT_FOUND = "WINDOW_NOT_FOUND"
    NOT_READY = "NOT_READY"
    SEARCH_PANEL_OPEN = "SEARCH_PANEL_OPEN"
    WECHAT_NOT_FRONTMOST = "WECHAT_NOT_FRONTMOST"
    NO_WINDOW = "NO_WINDOW"
    NO_SCREENSHOT = "NO_SCREENSHOT"

    # --- 联系人定位 ---
    CONTACT_NOT_FOUND = "CONTACT_NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    CONTACT_NOT_CONFIRMED = "CONTACT_NOT_CONFIRMED"

    # --- 消息读写 ---
    EMPTY_RESULT = "EMPTY_RESULT"
    EMPTY_MESSAGE = "EMPTY_MESSAGE"
    MESSAGE_TOO_LONG = "MESSAGE_TOO_LONG"
    DUPLICATE_BLOCKED = "DUPLICATE_BLOCKED"
    NOT_CONFIRMED = "NOT_CONFIRMED"
    SEND_UNVERIFIED = "SEND_UNVERIFIED"

    # --- 并发 ---
    BUSY = "BUSY"

    # --- 兜底 ---
    NOT_FOUND = "NOT_FOUND"
    TOOL_ERROR = "TOOL_ERROR"

    def __str__(self) -> str:  # 便于日志与旧代码按 str 使用
        return self.value


#: 引擎状态 → 错误码。缺省映射到 TOOL_ERROR，避免 `.upper()` 造出未登记的新码。
STATE_TO_ERR = {
    "wechat_not_running": Err.WECHAT_NOT_RUNNING,
    "window_not_found": Err.WINDOW_NOT_FOUND,
    "not_ready": Err.NOT_READY,
    "search_panel_open": Err.SEARCH_PANEL_OPEN,
}


def from_state(status: str) -> str:
    """把 UIState.status 翻成登记过的错误码。"""
    err = STATE_TO_ERR.get(status)
    return (err or Err.TOOL_ERROR).value


def all_codes() -> list[str]:
    """列出全部错误码，供文档生成与测试断言使用。"""
    return sorted(e.value for e in Err)
