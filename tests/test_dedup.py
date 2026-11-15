#!/usr/bin/env python3
"""dedup 模块的等价性测试（纯逻辑，不依赖微信界面）。

覆盖真实场景中的四类误判：
  1. 同人连发两条相同文本 —— 第二条必须上报，不能被当成重复吞掉
  2. 同一窗口重复轮询 —— 不得重复上报
  3. 新增一条 —— 只上报新增那条
  4. 水位线滚出可见区 —— 显式标记 watermark_lost，不静默出错

运行: .venv-rpa/bin/python test_dedup.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from argus.dedup import WatermarkStore, advance, mark  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def msg(sender, text, ts=None):
    return {"sender": sender, "text": text, "timestamp": ts}


def fresh_store():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)
    return WatermarkStore(path)


# ------------------------------------------------------------------ 用例 1
print("\n== 用例 1: 同人连发两条「好的」==")
store = fresh_store()
win1 = [msg("张三", "在吗", "10:00"), msg("自己", "在", "10:01")]
d = advance("测试群", mark("测试群", win1), store)
check("首次建立基线", d.baseline and len(d.fresh) == 2, f"baseline={d.baseline} n={len(d.fresh)}")

# 对方连发两条完全相同的「好的」，时间戳相同
win2 = win1 + [msg("张三", "好的", "10:02"), msg("张三", "好的", "10:02")]
marked2 = mark("测试群", win2)
idents = [m.identity for m in marked2 if m.payload["text"] == "好的"]
check("两条「好的」identity 不同", len(set(idents)) == 2, f"idents={idents}")

d = advance("测试群", marked2, store)
texts = [m["text"] for m in d.messages]
check("两条「好的」都被上报", texts.count("好的") == 2, f"fresh={texts}")

# ------------------------------------------------------------------ 用例 2
print("\n== 用例 2: 同一窗口重复轮询 ==")
d = advance("测试群", mark("测试群", win2), store)
check("重复轮询无新增", len(d.fresh) == 0, f"fresh={[m.payload['text'] for m in d.fresh]}")

# ------------------------------------------------------------------ 用例 3
print("\n== 用例 3: 新增一条消息 ==")
win3 = win2 + [msg("自己", "收到", "10:03")]
d = advance("测试群", mark("测试群", win3), store)
check("只上报新增一条", len(d.fresh) == 1 and d.fresh[0].payload["text"] == "收到",
      f"fresh={[m.payload['text'] for m in d.fresh]}")

# ------------------------------------------------------------------ 用例 4
print("\n== 用例 4: 水位线滚出可见区 ==")
win4 = [msg("李四", "新话题 1", "11:00"), msg("李四", "新话题 2", "11:01")]
d = advance("测试群", mark("测试群", win4), store)
check("标记 watermark_lost", d.watermark_lost, f"lost={d.watermark_lost}")
check("整窗作为候选返回", len(d.fresh) == 2, f"n={len(d.fresh)}")

# ------------------------------------------------------------------ 用例 5
print("\n== 用例 5: 时间戳识别波动（降级 weak 匹配）==")
store = fresh_store()
w1 = [msg("王五", "报价多少", "14:20"), msg("自己", "稍等", "14:21")]
advance("客户A", mark("客户A", w1), store)
# 第二轮时间戳全部丢失（Vision OCR 漏检的典型表现）
w2 = [msg("王五", "报价多少", None), msg("自己", "稍等", None), msg("王五", "304 的", None)]
d = advance("客户A", mark("客户A", w2), store)
texts = [m["text"] for m in d.messages]
check("降级匹配生效", d.reused_weak, f"reused={d.reused_weak}")
check("只上报新消息", texts == ["304 的"], f"fresh={texts}")

# ------------------------------------------------------------------ 用例 6
print("\n== 用例 6: 不同会话互不干扰 ==")
store = fresh_store()
advance("会话甲", mark("会话甲", [msg("A", "1")]), store)
d = advance("会话乙", mark("会话乙", [msg("A", "1")]), store)
check("新会话走基线", d.baseline, f"baseline={d.baseline}")

# ------------------------------------------------------------------ 用例 7
print("\n== 用例 7: 空窗口不改水位线 ==")
store = fresh_store()
advance("空窗", mark("空窗", [msg("A", "hi")]), store)
before = store.get("空窗").identity
d = advance("空窗", [], store)
after = store.get("空窗").identity
check("空窗口被安全跳过", before == after and len(d.fresh) == 0)

# ------------------------------------------------------------------ 用例 8
print("\n== 用例 8: 持久化往返 ==")
fd, path = tempfile.mkstemp(suffix=".json")
os.close(fd)
os.unlink(path)
s1 = WatermarkStore(path)
advance("持久化", mark("持久化", [msg("A", "x", "09:00")]), s1)
s1.save()
s2 = WatermarkStore(path)
d = advance("持久化", mark("持久化", [msg("A", "x", "09:00")]), s2)
check("重启后水位线仍有效", len(d.fresh) == 0 and not d.baseline,
      f"fresh={len(d.fresh)} baseline={d.baseline}")

print("\n" + "=" * 56)
if FAILS:
    print(f"失败 {len(FAILS)} 项: {FAILS}")
    sys.exit(1)
print("全部通过")
