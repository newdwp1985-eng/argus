#!/usr/bin/env python3
"""MCP 服务启动器。

存在的理由：MCP 客户端的工作目录通常不是项目根，直接 `-m argus.mcp_server`
会因为找不到包而失败。这里按脚本自身位置补齐 sys.path，
因此可以作为 MCP 配置里的 command 直接使用，无需设置 PYTHONPATH。

用法（写进 ~/.workbuddy/mcp.json）：
    {"command": "/path/to/.venv/bin/python", "args": ["/path/to/run_server.py"]}
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from argus.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
