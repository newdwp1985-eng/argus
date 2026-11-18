"""命令行入口，便于在 WorkBuddy 之外独立验证每个工具。

用法：
    python -m argus.cli doctor            # 环境诊断（不碰屏幕，任何环境可跑）
    python -m argus.cli self-test         # 纯逻辑自检（同上）
    python -m argus.cli status
    python -m argus.cli list
    python -m argus.cli search "文件传输助手"
    python -m argus.cli history "文件传输助手" --limit 20
    python -m argus.cli latest --limit 10
    python -m argus.cli monitor --seconds 30
    python -m argus.cli send "文件传输助手" "文本" --confirm

两条「不需要屏幕」的命令刻意写在最前面：任何人都能先确认环境与代码是否正常，
再去判断失败是不是微信没开、权限没给。工具函数的导入也推迟到真正用到时，
这样缺依赖的机器上 doctor 与 self-test 依然能跑完。
"""

from __future__ import annotations

import argparse
import json
import sys


def _tools():
    """按需导入工具层：它依赖 Apple 框架与科学计算库，不是所有命令都需要。"""
    from . import tools

    return tools


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="argus", description="Argus 视觉 RPA CLI")
    parser.add_argument("--version", action="store_true", help="打印版本号后退出")
    sub = parser.add_subparsers(dest="cmd")

    p_doc = sub.add_parser("doctor", help="环境诊断：平台 / Python / 依赖 / 微信运行状态")
    p_doc.add_argument("--strict", action="store_true", help="环境不就绪时以非零码退出")
    p_doc.add_argument("--no-runtime", action="store_true", help="跳过运行期探测（不碰屏幕）")

    sub.add_parser("self-test", help="纯逻辑自检：不碰屏幕、不依赖第三方库")

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

    if args.version:
        from . import __version__

        print(__version__)
        return 0

    if args.cmd is None:
        parser.print_help()
        return 0

    # --- 不需要屏幕的两条命令 ---
    if args.cmd == "doctor":
        from . import env

        report = env.summary()
        if not args.no_runtime:
            report["runtime"] = env.runtime_summary()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        # 诊断「跑完了」就算成功；是否就绪写在 JSON 的 ready 里。
        # 只有显式 --strict 才把「未就绪」当作退出码失败，供 CI 使用。
        return 0 if (report["ready"] or not args.strict) else 1

    if args.cmd == "self-test":
        from . import selftest

        return selftest.main()

    # --- 需要微信与屏幕的命令 ---
    tools = _tools()

    if args.cmd == "status":
        res = tools.get_wechat_status()
    elif args.cmd == "list":
        res = tools.get_chat_list()
    elif args.cmd == "search":
        res = tools.search_contact(args.name)
    elif args.cmd == "history":
        res = tools.get_chat_history(args.name, args.limit, args.deep, args.scrolls)
    elif args.cmd == "latest":
        res = tools.get_latest_messages(args.limit)
    elif args.cmd == "monitor":
        res = tools.monitor_new_messages(args.seconds, args.interval, args.once)
    elif args.cmd == "send":
        res = tools.send_message(args.name, args.text, confirm=args.confirm)
    elif args.cmd == "shot":
        res = tools.get_screenshot()
    elif args.cmd == "ocr":
        res = tools.get_ocr_result()
    else:
        parser.print_help()
        return 2

    print(res.to_json())
    return 0 if res.success else 1


if __name__ == "__main__":
    sys.exit(main())
