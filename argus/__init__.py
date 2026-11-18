"""Argus — 面向自绘界面的视觉 RPA 适配层。

当无障碍树读不出内容时（微信 4.x、抖音、多数 Qt/CEF 界面），
改用截图 + Vision OCR 还原界面语义，并给出可验证的操作回执。

对外提供三个等价入口：MCP（stdio）、CLI、Python API。

注意：``TOOLS`` 采用惰性导出。工具层会连带引入 pyobjc / Pillow / numpy，
而在没有这些库的机器上，「诊断环境」和「跑纯逻辑自检」仍然应当可用 ——
所以只有在真正访问 ``argus.TOOLS`` 时才去加载它。
"""

from .result import ToolResult, fail, ok

__all__ = ["TOOLS", "ToolResult", "ok", "fail"]
__version__ = "1.0.1"


def __getattr__(name: str):
    if name == "TOOLS":
        from .tools import TOOLS

        return TOOLS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
