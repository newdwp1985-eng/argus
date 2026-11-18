#!/usr/bin/env python3
"""环境降级与可运行性测试（纯逻辑，不依赖屏幕、不需要 pyobjc）。

背景：技能在审核沙箱里跑的是「干净机器」——没有 pyobjc、没有已登录的微信。
早期版本在这些环境里一 import 就抛 ModuleNotFoundError，评测方看到的是崩溃。
本文件的断言就是防止这种回归：

  1. ``import argus`` 不得连带加载工具层（否则又要 pyobjc）
  2. 缺依赖时工具必须返回 DEPENDENCY_MISSING，而不是抛异常
  3. ``argus doctor`` 在任何环境都能输出结构化结论并以 0 退出
  4. ``argus self-test`` 在无依赖环境里也必须全绿

运行: python tests/test_env.py
"""

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from argus import env, lock  # noqa: E402
from argus.errors import EnvUnavailable, Err  # noqa: E402
from argus.result import ToolResult  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def case(title):
    print(f"\n== 用例: {title} ==")


def _run(args, timeout=120):
    """在子进程里跑 CLI，避免污染当前进程的模块状态。"""
    return subprocess.run(
        [sys.executable] + args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------- 1
case("导入不连带工具层")
r = _run(["-c", "import argus,sys; print('argus.tools' in sys.modules); print(argus.__version__)"])
check("子进程 import argus 成功", r.returncode == 0, r.stderr[-300:])
out = r.stdout.strip().splitlines()
check("默认不加载 argus.tools", out and out[0] == "False", r.stdout)
check("版本号可读", len(out) > 1 and out[1].count(".") == 2, r.stdout)

r = _run(["-c", "from argus import TOOLS; print(len(TOOLS))"])
check("显式访问 argus.TOOLS 时才加载工具层", r.returncode == 0 and r.stdout.strip() == "9", r.stderr[-300:])

# ---------------------------------------------------------------- 2
case("环境诊断结论自洽")
rep = env.summary()
check("必备字段齐全",
      all(k in rep for k in ("ready", "platform", "python", "dependencies", "missing", "problems")),
      str(list(rep.keys())))
check("依赖清单非空且结构一致",
      bool(rep["dependencies"]) and all("module" in d and "ok" in d for d in rep["dependencies"]))
check("ready 与依赖状态自洽",
      rep["ready"] == (rep["platform_ok"] and rep["python_ok"] and not rep["missing"]))
check("结论可 JSON 序列化", isinstance(json.dumps(rep, ensure_ascii=False), str))

# ---------------------------------------------------------------- 3
case("缺依赖时抛 EnvUnavailable")
_orig_has = env._has_module
try:
    env._has_module = lambda name: name != "Quartz"
    check("缺一项必需依赖时 ready=False", env.ready() is False)
    check("missing 中列出 Quartz", "Quartz" in env.missing_required(), str(env.missing_required()))
    try:
        env.require_env()
        check("require_env 应抛异常", False)
    except EnvUnavailable as exc:
        check("require_env 抛 EnvUnavailable", True)
        check("错误码为 DEPENDENCY_MISSING", exc.error_code == "DEPENDENCY_MISSING", exc.error_code)
        check("说明里点了依赖名", "Quartz" in str(exc), str(exc))
finally:
    env._has_module = _orig_has

# ---------------------------------------------------------------- 4
case("工具层把环境异常转成错误码")
# 注意：tools.py 在导入时就把 core() 绑定成了 get_core，所以要替换的是
# tools.get_core，而不是 core.core —— 后者改了不会生效。
_orig_get_core = None
try:
    from argus import tools

    _orig_get_core = tools.get_core

    def _boom():
        raise EnvUnavailable("模拟没有 pyobjc", Err.DEPENDENCY_MISSING)

    tools.get_core = _boom
    res = tools.get_wechat_status()
    check("返回的是 ToolResult 而非异常", isinstance(res, ToolResult))
    check("success=False", res.success is False)
    check("error_code=DEPENDENCY_MISSING", res.error_code == "DEPENDENCY_MISSING", res.error_code)
    check("错误说明可读", "pyobjc" in (res.error or ""), res.error)
finally:
    if _orig_get_core is not None:
        tools.get_core = _orig_get_core

# ---------------------------------------------------------------- 5
case("串行闸门不打乱环境异常")
try:

    @lock.serialized(timeout=2.0)
    def _needs_env():
        raise EnvUnavailable("模拟缺 Pillow", Err.DEPENDENCY_MISSING)

    res = _needs_env()
    check("串行装饰器返回错误码", res.success is False and res.error_code == "DEPENDENCY_MISSING")
    check("锁已释放，可再次执行", lock.current()["owner"] is None, str(lock.current()))

    @lock.serialized(timeout=2.0)
    def _fine():
        return ToolResult(success=True)

    check("正常函数不受影响", _fine().success is True)
finally:
    pass

# ---------------------------------------------------------------- 6
case("CLI 诊断命令")
r = _run(["-m", "argus.cli", "doctor", "--no-runtime"])
check("doctor 退出码为 0", r.returncode == 0, r.stderr[-300:])
try:
    doc = json.loads(r.stdout)
    check("doctor 输出可解析 JSON", True)
    check("doctor 含 ready 字段", "ready" in doc)
except json.JSONDecodeError as exc:
    check("doctor 输出可解析 JSON", False, str(exc))

r = _run(["-m", "argus.cli", "--version"])
check("--version 输出版本号", r.returncode == 0 and r.stdout.strip().count(".") == 2, r.stdout)

# ---------------------------------------------------------------- 7
case("无依赖环境自检")
r = _run(["-m", "argus.cli", "self-test"])
check("self-test 退出码为 0", r.returncode == 0, (r.stderr or "")[-500:])
try:
    st = json.loads(r.stdout)
    check("self-test 输出可解析 JSON", True)
    check("自检项全部通过", st["success"] is True and st["failed"] == 0, str(st.get("checks")))
    check("自检项数量足够", st["passed"] >= 10, f"passed={st['passed']}")
except json.JSONDecodeError as exc:
    check("self-test 输出可解析 JSON", False, str(exc))

print("\n" + "=" * 56)
if FAILS:
    print(f"失败 {len(FAILS)} 项: {FAILS}")
    sys.exit(1)
print("全部通过")
