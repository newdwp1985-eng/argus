"""命令行入口，便于在 WorkBuddy 之外独立验证每个工具。

用法：
    python -m argus.cli status
    python -m argus.cli list
    python -m argus.cli search "文件传输助手"
    python -m argus.cli history "文件传输助手" --limit 20
    python -m argus.cli latest --limit 10
    python -m argus.cli monitor --seconds 30
    python -m argus.cli send "文件传输助手" "文本" --confirm
"""

from __future__ import annotations

import argparse
import json
import sys

from .tools import (
    get_chat_history,
    get_chat_list,
    get_latest_messages,
    get_ocr_result,
    get_screenshot,
    get_wechat_status,
    monitor_new_messages,
    search_contact,
    send_message,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="argus", description="Argus 微信自动化 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="检查微信状态")
    sub.add_parser("list", help="获取会话列表")

    p_search = sub.add_parser("search", help="搜索联系人")
    p_search.add_argument("name")

    p_hist = sub.add_parser("history", help="读取指定联系人聊天记录")
    p_hist.add_argument("name")
    p_hist.add_argument("--limit", type=int, default=30)
    p_hist.add_argument("--deep", action="store_true", help="向上滚动回溯，拼接多屏历史（需要微信在前台）")
    p_hist.add_argument("--scrolls", type=int, default=10, help="回溯屏数上限")

    p_latest = sub.add_parser("latest", help="读取当前聊天最新消息")
    p_latest.add_argument("--limit", type=int, default=10)

    p_mon = sub.add_parser("monitor", help="轮询监听新消息")
    p_mon.add_argument("--seconds", type=int, default=30)
    p_mon.add_argument("--interval", type=float, default=3.0)
    p_mon.add_argument("--once", action="store_true", help="只抽一次样立即返回")

    p_send = sub.add_parser("send", help="发送文字消息（必须 --confirm）")
    p_send.add_argument("name")
    p_send.add_argument("text")
    p_send.add_argument("--confirm", action="store_true")

    sub.add_parser("shot", help="保存当前窗口截图")
    sub.add_parser("ocr", help="输出当前窗口 OCR 元素")

    args = parser.parse_args(argv)

    if args.cmd == "status":
        res = get_wechat_status()
    elif args.cmd == "list":
        res = get_chat_list()
    elif args.cmd == "search":
        res = search_contact(args.name)
    elif args.cmd == "history":
        res = get_chat_history(args.name, args.limit, args.deep, args.scrolls)
    elif args.cmd == "latest":
        res = get_latest_messages(args.limit)
    elif args.cmd == "monitor":
        res = monitor_new_messages(args.seconds, args.interval, args.once)
    elif args.cmd == "send":
        res = send_message(args.name, args.text, confirm=args.confirm)
    elif args.cmd == "shot":
        res = get_screenshot()
    elif args.cmd == "ocr":
        res = get_ocr_result()
    else:
        parser.print_help()
        return 2

    print(res.to_json())
    return 0 if res.success else 1


if __name__ == "__main__":
    sys.exit(main())
