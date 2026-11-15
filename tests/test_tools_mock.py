"""tools.py 的纯逻辑测试：不碰真机，全部用替身。

覆盖三块最易回归的地方：
1. 联系人模糊匹配（列表昵称被 UI 截断是常态，匹配错就会发错人）
2. 发送前的四道校验（顺序与错误码都必须稳定，上游按码分支）
3. 并发闸门（两个调用重叠时必须让其中一个明确失败，而不是都往下走）
"""

from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace

from argus import errors, lock, tools
from argus.errors import Err

FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- 1. 联系人匹配

def item(nickname):
    return SimpleNamespace(nickname=nickname, preview="", unread=0, timestamp="")


def test_match_contact():
    print("\n--- 联系人匹配 ---")

    hit, cands = tools._match_contact("张三", [item("张三"), item("李四")])
    check("精确命中唯一", hit is not None and hit.nickname == "张三")

    miss, cands = tools._match_contact("王五", [item("张三"), item("李四")])
    check("无命中返回 None", miss is None and not cands)

    # 微信列表把长昵称截断成「某某物流：天⋯.」，用户输入的是全名
    trunc, cands = tools._match_contact(
        "某某物流：天津分公司", [item("某某物流：天⋯."), item("张三")]
    )
    check("截断昵称按前缀命中", trunc is not None and trunc.nickname == "某某物流：天⋯.")
    check("截断命中不留候选", not cands, f"cands={cands}")

    amb, cands = tools._match_contact("张三", [item("张三"), item("张三")])
    check("同名两条判为歧义", amb is None and len(cands) == 2, f"cands={cands}")

    pre_amb, cands = tools._match_contact(
        "某某物流：天津分公司", [item("某某物流：天⋯."), item("某某物流：⋯.")]
    )
    check("前缀多命中判为歧义", pre_amb is None and len(cands) == 2, f"cands={cands}")

    empty, cands = tools._match_contact("   ", [item("张三")])
    check("空输入不做匹配", empty is None and not cands)

    check("省略号被归一化", tools._normalize("张三⋯") == "张三")


# ---------------------------------------------------------------- 2. 发送前校验

class FakeCore:
    """只满足前置校验所需的接口，任何真正操作界面都会显式失败。"""

    def __init__(self, frontmost=True):
        self.frontmost = frontmost
        self.touched = False

    def is_wechat_running(self):
        return True

    def ensure_frontmost(self, max_wait=15):
        return self.frontmost

    def perceive(self):
        self.touched = True
        raise AssertionError("不应走到操作界面这一步")


def test_send_guards():
    print("\n--- 发送前校验 ---")
    tools._recent_sends.clear()
    original = tools.get_core
    tools.get_core = lambda: FakeCore(frontmost=True)
    try:
        r = tools.send_message("张三", "你好")
        check("未授权拒绝", r.error_code == Err.NOT_CONFIRMED.value, f"code={r.error_code}")

        r = tools.send_message("张三", "   ", confirm=True)
        check("空消息拒绝", r.error_code == Err.EMPTY_MESSAGE.value, f"code={r.error_code}")

        r = tools.send_message("张三", "x" * (tools.MAX_TEXT_LEN + 1), confirm=True)
        check("超长拒绝", r.error_code == Err.MESSAGE_TOO_LONG.value, f"code={r.error_code}")

        # 先塞一条记录，模拟刚发过同样内容
        import hashlib

        key = ("张三", hashlib.sha256("重复内容".encode()).hexdigest()[:12])
        tools._recent_sends[key] = 10**12  # 未来时间戳，必然落在窗口内
        r = tools.send_message("张三", "重复内容", confirm=True)
        check("重复内容拒绝", r.error_code == Err.DUPLICATE_BLOCKED.value, f"code={r.error_code}")

        tools._recent_sends.clear()
        tools.get_core = lambda: FakeCore(frontmost=False)
        r = tools.send_message("张三", "全新内容", confirm=True)
        check("不在前台拒绝", r.error_code == Err.WECHAT_NOT_FRONTMOST.value, f"code={r.error_code}")

        check("拒绝路径未触碰界面", True)
    finally:
        tools.get_core = original
        tools._recent_sends.clear()


# ---------------------------------------------------------------- 3. 状态映射

def test_state_mapping():
    print("\n--- 状态映射 ---")
    cases = {
        "wechat_not_running": Err.WECHAT_NOT_RUNNING,
        "window_not_found": Err.WINDOW_NOT_FOUND,
        "not_ready": Err.NOT_READY,
        "search_panel_open": Err.SEARCH_PANEL_OPEN,
    }
    for state, expect in cases.items():
        got = errors.from_state(state)
        check(f"{state} 映射正确", got == expect.value, f"got={got}")

    got = errors.from_state("something_new")
    check("未知状态归为 TOOL_ERROR", got == Err.TOOL_ERROR.value, f"got={got}")

    codes = errors.all_codes()
    check("错误码无重复", len(codes) == len(set(codes)))
    check("BUSY 已登记", "BUSY" in codes)


# ---------------------------------------------------------------- 4. 并发闸门

def test_lock():
    print("\n--- 并发闸门 ---")
    started = threading.Event()
    release_evt = threading.Event()
    got = []

    @lock.serialized(timeout=1.0)
    def slow():
        started.set()
        release_evt.wait(5)
        return "done"

    def worker():
        got.append(slow())

    t = threading.Thread(target=worker)
    t.start()
    check("第一个调用已持锁", started.wait(3))

    r = slow()  # 同一时刻的第二个调用
    check("重叠调用被拒绝", r.success is False, f"success={r.success}")
    check("拒绝码为 BUSY", r.error_code == Err.BUSY.value, f"code={r.error_code}")

    release_evt.set()
    t.join(5)
    check("首个调用正常完成", got == ["done"], f"got={got}")

    # 锁释放后可再次获取
    @lock.serialized(timeout=1.0)
    def quick():
        return "ok"

    check("释放后可重入", quick() == "ok")

    # 同一线程内嵌套不应死锁
    @lock.serialized(timeout=1.0)
    def outer():
        return inner()

    @lock.serialized(timeout=1.0)
    def inner():
        return "nested"

    check("同线程嵌套不死锁", outer() == "nested")


# ---------------------------------------------------------------- 5. 配置

def test_config():
    print("\n--- 配置外置 ---")
    from argus import config

    check("配置文件存在", config.describe()["exists"])
    check("取已存在的键", config.cfg_float("scroll.safe_lines", -1) == 8.0)
    check("缺失键回落默认", config.cfg("nope.nothing", "fb") == "fb")
    check("缺文件路径返回空字典", config._read("/definitely/not/here.json") == {})

    # 环境变量可指向外部配置
    old = os.environ.get(config.ENV_KEY)
    os.environ[config.ENV_KEY] = "/tmp/argus-not-exist.json"
    try:
        config.load(force=True)
        check("外部配置缺失时回落默认", config.cfg_int("limits.max_text_len", 77) == 77)
    finally:
        if old is None:
            os.environ.pop(config.ENV_KEY, None)
        else:
            os.environ[config.ENV_KEY] = old
        config.load(force=True)


if __name__ == "__main__":
    test_match_contact()
    test_send_guards()
    test_state_mapping()
    test_lock()
    test_config()

    print("\n" + "=" * 56)
    if FAILS:
        print(f"失败 {len(FAILS)} 项: {FAILS}")
        sys.exit(1)
    print("全部通过")
