"""运行环境判定。

这个模块只依赖标准库，因此**在任何 Python 环境下都能导入成功** ——
包括没有 pyobjc、没有 numpy、甚至不是 macOS 的环境。

存在的理由：视觉 RPA 的前提条件很高（macOS、已登录微信、屏幕录制权限、
四个 Apple 框架绑定、三个科学计算库）。缺任何一项，后续每一步都会失败。
与其让调用方撞上一段 traceback，不如在入口处明确回答三个问题：

1. 这台机器能不能跑 Argus（``ready``）；
2. 不能跑的话，缺的是哪一样（``missing``，每条都带安装命令）；
3. 缺这一项会导致哪些工具不可用（``blocked_tools``）。

抛错策略：本模块自身**不抛异常**，只产出结构化结论；需要中断调用链时，
由调用方执行 ``require_env()`` 让它抛出 ``EnvUnavailable``。
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
from typing import Any, Dict, List, Optional

from .errors import EnvUnavailable, Err

#: 支持的最低 Python 版本。与 install.sh 的 PY_MIN 保持一致：安装脚本拦在 3.10，
#: 这里的判定不能比它更宽松，否则会出现「装的时候被拒、诊断却说就绪」的矛盾。
MIN_PYTHON = (3, 10)

#: 依赖清单。``module`` 是 import 时用的名字，``package`` 是 pip 装的名字。
DEPENDENCIES: tuple[dict[str, Any], ...] = (
    {
        "module": "AppKit",
        "package": "pyobjc-framework-Cocoa",
        "required": True,
        "reason": "枚举进程、读写剪贴板、读取前台应用",
    },
    {
        "module": "Quartz",
        "package": "pyobjc-framework-Quartz",
        "required": True,
        "reason": "窗口枚举与 CGEvent 合成输入（点击、滚轮、按键）",
    },
    {
        "module": "Vision",
        "package": "pyobjc-framework-Vision",
        "required": True,
        "reason": "本地 OCR（VNRecognizeTextRequest）",
    },
    {
        "module": "PIL",
        "package": "Pillow",
        "required": True,
        "reason": "截图裁剪、放大与像素读取",
    },
    {
        "module": "numpy",
        "package": "numpy",
        "required": True,
        "reason": "OCR 元素聚类与墨色统计",
    },
    {
        "module": "scipy",
        "package": "scipy",
        "required": True,
        "reason": "布局解析中的连通域标记（ndimage）",
    },
    {
        "module": "mcp",
        "package": "mcp[cli]",
        "required": False,
        "reason": "仅 MCP 入口需要；CLI 与 Python API 不需要",
    },
)

#: 缺少某个模块时，具体哪些工具会不可用。用于让调用方提前知道影响面。
DEPENDENCY_IMPACT: dict[str, list[str]] = {
    "AppKit": ["get_wechat_status", "send_message", "*"],
    "Quartz": ["get_wechat_status", "get_chat_history", "send_message", "*"],
    "Vision": ["get_ocr_result", "get_chat_history", "get_latest_messages", "*"],
    "PIL": ["get_screenshot", "get_ocr_result", "*"],
    "numpy": ["get_chat_history", "get_latest_messages", "*"],
    "scipy": ["get_chat_history", "get_latest_messages", "*"],
    "mcp": ["MCP 服务入口"],
}


def _has_module(name: str) -> bool:
    """模块是否可导入。只查 spec，不真的 import —— 避免加载 .so 带来的副作用。"""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def python_version() -> str:
    return "{}.{}.{}".format(*sys.version_info[:3])


def python_ok() -> bool:
    return sys.version_info[:2] >= MIN_PYTHON


def platform_ok() -> bool:
    return sys.platform == "darwin"


def dependency_checks() -> List[Dict[str, Any]]:
    """逐项检查依赖，返回可直接写进 JSON 的列表。"""
    out: List[Dict[str, Any]] = []
    for dep in DEPENDENCIES:
        present = _has_module(dep["module"])
        item: Dict[str, Any] = {
            "module": dep["module"],
            "package": dep["package"],
            "required": dep["required"],
            "ok": present,
            "reason": dep["reason"],
        }
        if not present:
            item["install"] = (
                "pip install " + dep["package"]
                if dep["module"] != "mcp"
                else 'pip install "mcp[cli]>=1.9,<2"'
            )
            item["blocks"] = DEPENDENCY_IMPACT.get(dep["module"], [])
        out.append(item)
    return out


def external_tools() -> Dict[str, Any]:
    """非 Python 依赖：cliclick 是可选加速项，缺失时自动降级为 CGEvent。"""
    path = shutil.which("cliclick") or next(
        (p for p in ("/opt/homebrew/bin/cliclick", "/usr/local/bin/cliclick") if os.path.exists(p)),
        None,
    )
    return {
        "cliclick": {
            "found": bool(path),
            "path": path,
            "required": False,
            "note": "缺失时自动降级为 CGEvent 点击，功能不受影响",
        }
    }


def missing_required() -> List[str]:
    return [d["module"] for d in dependency_checks() if d["required"] and not d["ok"]]


def ready() -> bool:
    """硬件与软件前提是否全部满足（不含「微信是否正在运行」）。"""
    return platform_ok() and python_ok() and not missing_required()


def summary() -> Dict[str, Any]:
    """结构化诊断结论。任何环境下都可调用，不抛异常。"""
    deps = dependency_checks()
    missing = [d["module"] for d in deps if d["required"] and not d["ok"]]
    missing_optional = [d["module"] for d in deps if not d["required"] and not d["ok"]]

    problems: List[str] = []
    if not platform_ok():
        problems.append(
            f"当前平台是 {sys.platform}，Argus 依赖 macOS 的 Quartz/Vision/AppKit，无法在非 macOS 上运行"
        )
    if not python_ok():
        problems.append(
            f"Python {python_version()} 低于最低要求 {MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
        )
    if missing:
        problems.append("缺少必需依赖：" + "、".join(missing))
    if missing_optional:
        problems.append("缺少可选依赖：" + "、".join(missing_optional) + "（不影响 CLI 与 Python API）")

    blocked: List[str] = []
    for d in deps:
        if d["required"] and not d["ok"]:
            for t in DEPENDENCY_IMPACT.get(d["module"], []):
                if t not in blocked:
                    blocked.append(t)

    return {
        "ready": ready(),
        "platform": sys.platform,
        "platform_ok": platform_ok(),
        "python": python_version(),
        "python_ok": python_ok(),
        "executable": sys.executable,
        "dependencies": deps,
        "external_tools": external_tools(),
        "missing": missing,
        "missing_optional": missing_optional,
        "blocked_tools": blocked,
        "problems": problems,
        "install_hint": (
            "pip install -r requirements.txt"
            if (missing and platform_ok())
            else None
        ),
    }


def runtime_summary() -> Dict[str, Any]:
    """在依赖齐备时顺带探测运行期状态；依赖缺失时返回 ``probed: false``。"""
    if not ready():
        return {"probed": False, "reason": "依赖未齐备，跳过运行期探测", "wechat_running": None}

    try:
        from .core import core as get_core

        c = get_core()
        running = c.is_wechat_running()
        info: Dict[str, Any] = {
            "probed": True,
            "wechat_running": running,
            "frontmost": c.frontmost_is_wechat() if running else None,
            "click_backend": "cliclick" if not c.clicker.degraded else "cgevent(fallback)",
        }
        if running:
            _result, state = c.perceive()
            info["ui_status"] = state.status
            info["ui_detail"] = state.detail
            info["ui_usable"] = state.usable
        return info
    except Exception as exc:  # noqa: BLE001 探测失败不应让诊断本身失败
        return {
            "probed": True,
            "wechat_running": None,
            "probe_error": f"{type(exc).__name__}: {exc}",
        }


def env_error() -> Optional[tuple[str, Err]]:
    """返回第一处阻断性问题的（说明, 错误码）；环境正常时返回 None。"""
    if not platform_ok():
        return (
            f"当前平台为 {sys.platform}，Argus 依赖 macOS 的 Quartz/Vision/AppKit，"
            "在非 macOS 环境下只能运行不依赖屏幕的纯逻辑部分（可用 `argus self-test` 验证）",
            Err.PLATFORM_UNSUPPORTED,
        )
    if not python_ok():
        return (
            f"Python {python_version()} 低于最低要求 {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
            Err.DEPENDENCY_MISSING,
        )
    missing = missing_required()
    if missing:
        return (
            "缺少必需依赖：" + "、".join(missing) + "；请执行 pip install -r requirements.txt",
            Err.DEPENDENCY_MISSING,
        )
    return None


def require_env() -> None:
    """环境不可用时抛 ``EnvUnavailable``，由工具层转成结构化错误码。"""
    problem = env_error()
    if problem is not None:
        message, code = problem
        raise EnvUnavailable(message, code)
