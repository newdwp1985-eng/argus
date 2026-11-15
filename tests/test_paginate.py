#!/usr/bin/env python3
"""paginate 拼接逻辑测试：用假 core 模拟滚动，不触碰真实微信。

验证四件事：
  1. 从最新位置向上回溯，能一路读到会话开头
  2. 拼接结果不重复、不丢条、顺序正确
  3. 到达顶部时正确标记 reached_top
  4. 一次翻页跨过整屏时，能识别为缺口而不是假装连续

运行: .venv-rpa/bin/python test_paginate.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from argus.paginate import merge_page, scroll_history  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- 假界面

class FakeMsg:
    def __init__(self, sender, text, ts=None):
        self.sender = sender
        self.text = text
        self.timestamp = ts
        self.sender_type = "SenderType.OTHER"


class FakeRect:
    def __init__(self):
        self.width = 700
        self.height = 600
        self.x = 0
        self.y = 0


class FakeResult:
    def __init__(self, chat_name, messages):
        self.chat_name = chat_name
        self.messages = messages
        self.window_rect = FakeRect()


class FakeState:
    usable = True
    status = "ok"
    detail = ""


class FakeCore:
    """模拟滚动：offset 是当前窗口顶部在完整语料中的下标。

    lines>0 表示向上滚（看到更早的内容，offset 减小）。
    ``jump_over`` > 0 时，大步长会额外跳过若干条，用于模拟微信跳页。
    """

    def __init__(self, corpus, window=4, max_lines_per_scroll=None, jump_over=0):
        self.corpus = corpus
        self.window = window
        self.offset = max(0, len(corpus) - window)
        self.max_step = max_lines_per_scroll
        self.jump_over = jump_over
        self.scroll_calls = 0

    def perceive(self):
        end = min(len(self.corpus), self.offset + self.window)
        return FakeResult("测试会话", self.corpus[self.offset:end]), FakeState()

    def scroll(self, px, py, lines):
        self.scroll_calls += 1
        step = lines
        if self.max_step is not None:
            step = max(-self.max_step, min(self.max_step, step))
        if self.jump_over and abs(step) >= 5:
            step += self.jump_over if step > 0 else -self.jump_over
        # 向上滚 = offset 减小；向下滚 = offset 增大
        self.offset = max(0, min(len(self.corpus) - self.window, self.offset - step))

    def scroll_ratio(self, rx, ry, lines):
        self.scroll(0, 0, lines)

    def cursor_pos(self):
        return (0.0, 0.0)

    def move_cursor(self, x, y):
        pass


def corpus(n, prefix="消息"):
    return [FakeMsg("张三" if i % 2 else "自己", f"{prefix}{i:02d}") for i in range(n)]


# ---------------------------------------------------------------- 用例 1
print("\n== 用例 1: 20 条历史，窗口 4 条，回溯全程 ==")
core = FakeCore(corpus(20), window=4)
out = scroll_history(core, max_scrolls=40, lines_per_scroll=2, target_count=500)
texts = [m["text"] for m in out.messages]
check("读到会话开头", out.reached_top, f"reached_top={out.reached_top}")
check("条数完整（20 条）", len(texts) == 20, f"n={len(texts)}")
check("无重复", len(set(texts)) == len(texts), f"dups={[t for t in set(texts) if texts.count(t)>1]}")
check("顺序正确（旧→新）", texts == [f"消息{i:02d}" for i in range(20)], f"{texts[:4]}...{texts[-2:]}")
check("无缺口", not out.gaps, f"gaps={out.gaps}")
check("方向已校准", out.direction != 0, f"dir={out.direction}")

# ---------------------------------------------------------------- 用例 2
print("\n== 用例 2: 大窗口（一次读 6 条），回溯 10 条 ==")
core = FakeCore(corpus(10), window=6)
out = scroll_history(core, max_scrolls=20, lines_per_scroll=3, target_count=500)
texts = [m["text"] for m in out.messages]
check("条数完整", len(texts) == 10, f"n={len(texts)}")
check("无重复", len(set(texts)) == len(texts), f"n={len(texts)} uniq={len(set(texts))}")
check("顺序正确", texts == [f"消息{i:02d}" for i in range(10)], f"{texts}")

# ---------------------------------------------------------------- 用例 3
print("\n== 用例 3: 内容只有一屏，无需回溯 ==")
core = FakeCore(corpus(3), window=5)
out = scroll_history(core, max_scrolls=10, lines_per_scroll=2)
check("标记到顶", out.reached_top, f"reached_top={out.reached_top}")
check("返回现有 3 条", len(out.messages) == 3, f"n={len(out.messages)}")

# ---------------------------------------------------------------- 用例 4
print("\n== 用例 4: target_count 截断 ==")
core = FakeCore(corpus(30), window=4)
out = scroll_history(core, max_scrolls=40, lines_per_scroll=2, target_count=10)
check("达到目标即停", 10 <= len(out.messages) < 30, f"n={len(out.messages)}")
check("尾部是最新消息", out.messages[-1]["text"] == "消息29", f"last={out.messages[-1]['text']}")

# ---------------------------------------------------------------- 用例 5
print("\n== 用例 5: merge_page 重叠计算 ==")
acc = [{"sender": "A", "text": "1"}, {"sender": "A", "text": "2"},
       {"sender": "A", "text": "3"}, {"sender": "A", "text": "4"}]
page = [{"sender": "A", "text": "0"}, {"sender": "A", "text": "1"},
        {"sender": "A", "text": "2"}]
merged, k = merge_page(page, acc)
check("重叠 k=2", k == 2, f"k={k}")
check("合并后 5 条", len(merged) == 5, f"n={len(merged)}")
check("合并顺序正确", [m["text"] for m in merged] == ["0", "1", "2", "3", "4"],
      f"{[m['text'] for m in merged]}")

# 无重叠
merged2, k2 = merge_page([{"sender": "B", "text": "x"}], acc)
check("无重叠 k=0", k2 == 0, f"k={k2}")
check("无重叠时直接前置", len(merged2) == 5, f"n={len(merged2)}")

# ---------------------------------------------------------------- 用例 6
print("\n== 用例 6: 重复文本的锚点歧义 ==")
# 连续多条相同内容：锚点必须取最长匹配，不能错位
acc = [{"sender": "A", "text": "好的"}] * 3 + [{"sender": "A", "text": "结束"}]
page = [{"sender": "A", "text": "开始"}] + [{"sender": "A", "text": "好的"}] * 3
merged3, k3 = merge_page(page, acc)
check("重复文本取最长重叠", k3 == 3, f"k={k3}")
check("合并后 5 条", len(merged3) == 5, f"n={len(merged3)}")

# ---------------------------------------------------------------- 用例 7
print("\n== 用例 7: 跳页回退不污染结果 ==")
# 大步长会额外跳过 6 条，迫使出现 k=0 并触发回退重试。
# 这条用例防的是一个真实 bug：早期实现在合并后又回退重试，
# 导致同一屏内容被重复并入，输出里出现整段重复。
core = FakeCore(corpus(24), window=4, jump_over=6)
out = scroll_history(core, max_scrolls=40, lines_per_scroll=4, target_count=500)
texts = [m["text"] for m in out.messages]
n = len(texts)
dup_pair = None
for i in range(n):
    for j in range(i + 2, n):
        ln = min(n - i, n - j)
        if ln >= 2 and texts[i:i + ln] == texts[j:j + ln]:
            dup_pair = (i, j)
            break
    if dup_pair:
        break
check("无整段重复", dup_pair is None, f"重复起点 {dup_pair} | {texts}")
check("无乱序", texts == sorted(texts, key=lambda t: int(t[-2:])), f"{texts}")

print("\n" + "=" * 56)
if FAILS:
    print(f"失败 {len(FAILS)} 项: {FAILS}")
    sys.exit(1)
print("全部通过")