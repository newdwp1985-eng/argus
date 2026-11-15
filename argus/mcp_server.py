"""MCP Server：把微信工具接口层暴露为标准 MCP stdio 服务。

启动：
    /path/to/python -m argus.mcp_server
"""

from __future__ import annotations

import json
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from .result import ToolResult
from .tools import (
    get_chat_history as _get_chat_history,
    get_chat_list as _get_chat_list,
    get_latest_messages as _get_latest_messages,
    get_ocr_result as _get_ocr_result,
    get_screenshot as _get_screenshot,
    get_wechat_status as _get_wechat_status,
    monitor_new_messages as _monitor_new_messages,
    search_contact as _search_contact,
    send_message as _send_message,
)

mcp = FastMCP("argus")


def _payload(res: ToolResult) -> dict[str, Any]:
    """MCP 工具统一返回该字典；success=False 时由调用方（Agent）决定后续处理。"""
    return res.to_dict()


@mcp.tool()
def get_wechat_status() -> dict:
    """检查 Mac 微信客户端是否运行、窗口是否可自动化、当前界面是否处于可用状态。

    返回 window 边界、当前聊天名、点击后端（cliclick 或降级 CGEvent）。
    ui_status 不为 ok 时不要执行后续读写操作。
    """
    return _payload(_get_wechat_status())


@mcp.tool()
def get_chat_list() -> dict:
    """获取微信左侧会话列表：昵称、最后一条消息预览、未读数、时间戳。

    昵称可能被微信 UI 截断（带省略号），这是界面本身的显示限制。
    """
    return _payload(_get_chat_list())


@mcp.tool()
def search_contact(name: str) -> dict:
    """在会话列表中搜索联系人，只匹配不切换、不发送。

    命中多个时返回 AMBIGUOUS 与候选列表；未命中返回 NOT_FOUND 与当前列表。
    """
    return _payload(_search_contact(name))


@mcp.tool()
def get_chat_history(
    contact: str,
    limit: int = 30,
    deep: bool = False,
    max_scrolls: int = 10,
) -> dict:
    """读取指定联系人的聊天记录（会切换到该会话）。

    返回按时间顺序的消息列表，每条含 sender / sender_type / text / timestamp /
    message_id（伪消息 ID，可区分同一人连发的相同内容）/ occurrence。

    deep=False 只读当前可见区，速度快，不需要微信在前台。
    deep=True 向上滚动回溯并拼接多屏历史，能拿到可见区之外的消息；
    滚动是输入操作，要求微信在前台，无法置前时返回 WECHAT_NOT_FRONTMOST。
    """
    return _payload(_get_chat_history(contact, limit, deep, max_scrolls))


@mcp.tool()
def get_latest_messages(limit: int = 10) -> dict:
    """读取当前已打开聊天的最新消息，不切换会话。适合在发送后确认结果。"""
    return _payload(_get_latest_messages(limit))


@mcp.tool()
def monitor_new_messages(
    duration_seconds: int = 30, interval: float = 3.0, once: bool = True
) -> dict:
    """查看当前聊天有没有新消息，按水位线去重后返回增量。

    默认 once=True：只抽一次样就返回，耗时约 1-2 秒，适合由你按自己的节奏反复调用。
    若需要连续盯一段时间，设 once=False 并给 duration_seconds —— 该调用会一直阻塞
    到窗口结束，期间其他工具会返回 BUSY。

    这是轮询而非实时推送：两次轮询之间被界面挤出可见区的消息可能漏读，
    返回值中的 uncertainty_note 会说明该局限，不要把它当成可靠的实时送达。
    """
    return _payload(_monitor_new_messages(duration_seconds, interval, once))


@mcp.tool()
def send_message(contact: str, text: str, confirm: bool = False) -> dict:
    """向指定联系人发送文字消息。必须传入 confirm=True 表示已获人工授权。

    安全约束：联系人无法唯一确认、内容为空、30 秒内重复内容，都会被拒绝发送。
    返回 confirmed_in_ui 表示是否在发送后从界面读到了该消息。
    """
    return _payload(_send_message(contact, text, confirm))


@mcp.tool()
def get_screenshot() -> dict:
    """保存当前微信窗口截图到本地并返回文件路径，用于排障或人工核对。"""
    return _payload(_get_screenshot())


@mcp.tool()
def get_ocr_result() -> dict:
    """返回当前窗口全部 OCR 文本元素及坐标与置信度，用于布局校准和排障。"""
    return _payload(_get_ocr_result())


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
