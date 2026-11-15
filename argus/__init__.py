"""Argus — 面向自绘界面的视觉 RPA 适配层。

当无障碍树读不出内容时（微信 4.x、抖音、多数 Qt/CEF 界面），
改用截图 + Vision OCR 还原界面语义，并给出可验证的操作回执。

对外提供三个等价入口：MCP（stdio）、CLI、Python API。
"""

from .result import ToolResult, fail, ok
from .tools import TOOLS

__all__ = ["TOOLS", "ToolResult", "ok", "fail"]
__version__ = "1.0.0"
