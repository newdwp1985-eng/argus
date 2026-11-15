"""跨屏回溯读取：把可见区之外的历史消息拼接回来。

## 问题

单次截图只能拿到当前可见的几条消息。更早的历史需要向上滚动加载，
而桌面 OCR 每次读到的是一屏独立文本，没有共享 ID 可以对齐。

## 做法

1. **先归位**：向下滚到稳定，确保从最新消息开始，避免读到半屏。
2. **方向自校准**：不预设滚轮正负号。先试一个方向，观察旧的首条消息
   是否被推到屏幕下方（说明看到了更早的内容）；不行就反向。
   这样不必依赖写死的常量，换鼠标设备也能工作。
3. **逐屏上滚**：每屏解析后与已累积内容做**重叠对齐** —— 找最大的 k 使得
   新屏的尾部 k 条与累积结果的头部 k 条一致，然后把新屏的前段拼到前面。
4. **缺口显式化**：若两屏完全不重叠（k=0），说明一次滚动跳过了内容，
   记一条 gap 并降低步长重试，而不是假装连续拼接。
5. **到顶判定**：连续两屏内容完全相同，说明无法继续上滚，标记 reached_top。

## 已知边界

- 靠（发送者 + 正文前 20 字）做锚点。若两条消息发送者与开头完全相同，
  锚点会歧义；此时取**最长**的可行 k，偏向过度重叠（宁重叠不漏接）。
- 表情、图片、红包等无文本消息不会被 OCR 识别，因此它们不参与锚点，
  表现为「本屏消息条数少于肉眼所见」，拼接结果会缺这些条目。
- 滚动期间若有新消息到达，底部内容会变，但已读的上半部分不受影响。
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .config import cfg_float, cfg_int

# 数值来自 profile_default.json 的 scroll 段；配置文件缺失时回落到这里的默认值。
MAX_OVERLAP = cfg_int("scroll.max_overlap", 14)      # 锚点比对的最大条数
ANCHOR_TEXT_LEN = cfg_int("scroll.anchor_text_len", 16)  # 锚点骨架保留多少个字符
FUZZY_RATIO = cfg_float("scroll.fuzzy_ratio", 0.82)  # 骨架相似度阈值，低于此值判为不同消息
SAFE_SCROLL_LINES = cfg_int("scroll.safe_lines", 8)  # 单次滚动行数的安全上限
MAX_RETRIES = cfg_int("scroll.max_retries", 4)       # 单次回溯内允许的「缩小步长重试」次数
SETTLE_WAIT = cfg_float("scroll.settle_wait", 0.55)  # 每次滚动后等待渲染的秒数
SETTLE_TRIES = cfg_int("scroll.settle_tries", 3)     # 判定渲染稳定的最大重试次数

_PUNCT = set(
    " \t\n\r　，,。.、；;：:？！?!“”\"'‘’（）()【】[]《》〈〉<>…—–-~·|/\\*#@&%$"
)


def _skeleton(text: str) -> str:
    """把正文压成可比对的骨架。

    同一句话在不同屏的 OCR 结果会有细微差异，实测量到的三类噪声：
      · 标点被识别成不同形态（``镜头——`` / ``镜头—``）
      · 空格位置漂移（``机 身`` / ``机身``）
      · 大小写不一致（``S.s`` / ``S.S``）
      · emoji 被识别成孤立数字并粘在句首（``9 这表情``）
    这些差异让人眼一看就是同一条，精确字符串比较却判为不同，导致拼接全乱。
    因此锚点比较一律先用骨架：去空白、去标点符号、转小写、剥掉句首孤立数字。
    """
    t = " ".join((text or "").split())
    t = "".join(ch for ch in t if ch not in _PUNCT)
    t = t.lower()
    t = re.sub(r"^\d{1,2}(?=[\u4e00-\u9fff])", "", t)
    return t[:ANCHOR_TEXT_LEN]


def _key(msg: dict) -> str:
    """锚点键：只取正文骨架，刻意不含发送者。

    发送者判定在跨屏时不稳（同一条消息可能一会儿被认成「自己」、
    一会儿被认成「对方」，取决于绿色气泡检测是否命中），
    把它放进锚点会让本来能匹配的两条消息判为不同。
    """
    return _skeleton(str(msg.get("text") or ""))


def _same(a: dict, b: dict) -> bool:
    """判断两条消息是否是同一条（容忍 OCR 噪声）。"""
    ka, kb = _key(a), _key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    return difflib.SequenceMatcher(None, ka, kb).ratio() >= FUZZY_RATIO


def _sig(msgs: Sequence[dict]) -> str:
    return "|".join(_key(m) for m in msgs)


@dataclass
class PageResult:
    messages: List[dict] = field(default_factory=list)   # 时间升序
    pages_read: int = 0
    scrolls: int = 0
    gaps: List[dict] = field(default_factory=list)
    reached_top: bool = False
    direction: int = 0                                   # +1/-1，0 表示未能滚动
    chat_name: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.messages)


class _Reader:
    """对 core 的薄封装，统一「滚动 + 稳定后读取」。

    滚动焦点用窗口内相对比例表达，由 core.scroll_ratio 负责换算成物理像素。
    早期版本在这里直接把 window_rect 的逻辑点当物理像素传下去，
    焦点被算到左侧会话列表上，滚轮被列表吃掉 —— 表现为「滚了但内容不变」。
    """

    def __init__(self, core, focus_ratio: tuple[float, float], lines: int):
        self.core = core
        self.rx, self.ry = focus_ratio
        self.lines = lines

    def read(self) -> Optional[list]:
        result, state = self.core.perceive()
        if not state.usable:
            return None
        return [{"sender": m.sender, "text": m.text, "timestamp": m.timestamp,
                 "sender_type": str(getattr(m, "sender_type", "")).split(".")[-1]}
                for m in result.messages]

    def scroll(self, direction: int, lines: Optional[int] = None) -> None:
        self.core.scroll_ratio(self.rx, self.ry, direction * (lines or self.lines))

    def settle(self, max_tries: int = SETTLE_TRIES) -> Optional[list]:
        """滚动后等待内容稳定，返回稳定后的消息列表。"""
        prev = None
        cur = None
        for _ in range(max_tries):
            time.sleep(SETTLE_WAIT)
            cur = self.read()
            if cur is not None and _sig(cur) == _sig(prev or []):
                return cur
            prev = cur
        return cur


def _sim(a: dict, b: dict) -> float:
    ka, kb = _key(a), _key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    return difflib.SequenceMatcher(None, ka, kb).ratio()


def _overlap(older: Sequence[dict], acc: Sequence[dict]) -> int:
    """求 older 尾部与 acc 头部的最大重叠条数。

    ``older`` 是本次新读到的一屏（更早的内容），``acc`` 是已累积结果（时间升序）。
    返回 k：older[-k:] 与 acc[:k] 是同一段内容；返回 0 表示无法对齐。

    接受两种对齐依据：
      1. **接缝对齐**：末条匹配且整体相似度达标 —— 最可靠；
      2. **整体高重合**：末条因 OCR 差异没对上，但整段大多数条目逐字相同
         —— 用于长消息在两次识别中被切分得不一样的情形。

    早期版本只认第 1 种（末条必须匹配），实测在长消息、含 emoji 的消息上
    频繁失败，退化成缺口，导致回溯丢条。
    """
    limit = min(MAX_OVERLAP, len(older), len(acc))
    for k in range(limit, 0, -1):
        pairs = [(older[len(older) - k + i], acc[i]) for i in range(k)]
        sims = [_sim(a, b) for a, b in pairs]
        avg = sum(sims) / k
        exact = sum(1 for s in sims if s >= 1.0)

        if _same(older[len(older) - 1], acc[k - 1]) and avg >= 0.72:
            return k
        if exact >= max(2, int(k * 0.75)) and avg >= 0.85:
            return k
    return 0


def merge_page(page: Sequence[dict], acc: Sequence[dict]) -> tuple[list, int]:
    """把更早的一屏拼接到累积结果之前，返回 (新累积, 重叠条数)。

    重叠条数为 0 表示两屏之间找不到共同锚点 —— 调用方必须把这种情形
    当作可能漏读上报，不能当成正常拼接。
    """
    k = _overlap(page, acc)
    if k:
        return list(page[:-k]) + list(acc), k
    return list(page) + list(acc), 0


def scroll_history(
    core,
    max_scrolls: int = 10,
    lines_per_scroll: int = 4,
    target_count: int = 200,
    focus_ratio: tuple[float, float] = (0.60, 0.45),
) -> PageResult:
    """向上回溯读取历史消息。

    ``core`` 需已激活微信且处于目标会话。本函数只滚动与读取，不写任何内容。
    """
    result, state = core.perceive()
    if not state.usable:
        return PageResult(notes=[f"微信界面不可用：{state.status} {state.detail or ''}"])

    wr = result.window_rect
    # 单次滚动行数必须钳制：实测一次滚 30 行以上时，微信的响应会变得不可预测
    # （内容反而跳回更早的位置），8 行以内稳定可复现
    lines_per_scroll = max(1, min(int(lines_per_scroll), SAFE_SCROLL_LINES))
    rd = _Reader(core, focus_ratio, lines_per_scroll)

    out = PageResult(chat_name=result.chat_name or "")

    # 1) 归位：向下滚到稳定，确保起点是最新消息
    #    归位要用大步长，1 行不足以跨过一条消息的高度，会误判为「已在底部」
    stable = rd.settle()
    if stable is None:
        return PageResult(notes=["首次读取失败"])
    for _ in range(6):
        rd.scroll(-1, lines=SAFE_SCROLL_LINES)
        nxt = rd.settle()
        if nxt is None or _sig(nxt) == _sig(stable):
            break
        stable = nxt

    acc: List[dict] = list(stable)
    out.pages_read = 1
    last_sig = _sig(acc)

    # 2) 方向自校准：不预设滚轮正负号。
    #    步长必须足够大 —— 实测滚 1 行时 OCR 结果完全不变（内容未跨过消息边界），
    #    会误判成「滚动无效」。因此先按正常步长试，仍不动再放大 3 倍试一次。
    direction = 0
    for cand in (1, -1):
        for step in (lines_per_scroll, SAFE_SCROLL_LINES):
            before = rd.read()
            rd.scroll(cand, step)
            probe = rd.settle()
            if probe is not None and _sig(probe) != _sig(before or []):
                direction = cand
                # 这一屏已经读到了，直接参与拼接
                acc, k = merge_page(probe, acc)
                if k == 0:
                    out.gaps.append({"at_index": len(acc), "reason": "首屏未找到重叠锚点"})
                last_sig = _sig(probe)
                out.pages_read += 1
                out.scrolls += 1
                break
            # 该方向无位移，复位后再试
            rd.scroll(-cand, step)
        if direction:
            break

    if direction == 0:
        out.messages = acc
        out.reached_top = True
        out.notes.append("滚动无位移：可能已到达会话顶部，或界面未获得输入焦点")
        return out

    out.direction = direction

    # 3) 逐屏上滚
    width = lines_per_scroll
    still = 0
    retries = 0
    while out.scrolls < max_scrolls and len(acc) < target_count:
        rd.scroll(direction, width)
        page = rd.settle()
        out.scrolls += 1

        if page is None:
            out.notes.append("某一屏读取失败，已中止回溯")
            break

        sig = _sig(page)
        if sig == last_sig:
            # 内容完全没变。可能是已到顶，也可能是滚动事件被忽略，
            # 因此要求连续两次不动才判定到顶，避免把偶发丢事件当成到顶。
            still += 1
            if still >= 2:
                out.reached_top = True
                break
            continue
        still = 0

        # 先算重叠，再决定是否合并 —— 顺序不能反。
        # 若先 merge_page（它会在 k=0 时整屏前置），随后又走「回退重试」，
        # acc 里就会留下已前置的这一屏，产生整段重复。
        k = _overlap(page, acc)
        if k == 0:
            if width > 1 and retries < MAX_RETRIES:
                # 回退到翻页前的位置，缩小步长重来。
                # 这次没前进，不计入有效滚动次数。
                retries += 1
                out.scrolls -= 1
                rd.scroll(-direction, width)
                rd.settle()
                width = max(1, width - 1)
                continue
            # 步长已到下限仍无重叠：只能整屏前置，并记录缺口
            out.gaps.append(
                {
                    "at_index": len(acc),
                    "page_size": len(page),
                    "reason": "本次翻页与已读内容无共同锚点，可能跳过了消息",
                }
            )
            acc = list(page) + list(acc)
        else:
            acc = list(page[:-k]) + list(acc)

        out.pages_read += 1
        last_sig = sig

    out.messages = acc
    if len(acc) >= target_count:
        out.notes.append(f"已达到目标条数 {target_count}，停止回溯")
    if out.gaps:
        out.notes.append(
            f"检测到 {len(out.gaps)} 处锚点缺口，缺口处的消息可能缺失，请勿视为完整历史"
        )
    if not out.reached_top and out.scrolls >= max_scrolls:
        out.notes.append(f"已达单次回溯上限 {max_scrolls} 屏，更早的历史未读取")
    return out


def restore_bottom(core, lines: int = 8, max_tries: int = 24) -> bool:
    """把会话视图滚回最新位置，避免把界面留在历史中间。

    判定标准是连续两次滚动后内容不变（而不是单次比较），
    因为滚动有渲染延迟，单次相同可能只是还没画完，会误判成到底。
    """
    result, state = core.perceive()
    if not state.usable:
        return False
    prev = None
    still = 0
    for _ in range(max_tries):
        cur = _sig([{"text": m.text, "sender": m.sender} for m in result.messages])
        still = still + 1 if cur == prev else 0
        if still >= 2:
            return True
        prev = cur
        core.scroll_ratio(0.60, 0.45, -lines)
        time.sleep(0.6)
        result, state = core.perceive()
        if not state.usable:
            return False
    return False
