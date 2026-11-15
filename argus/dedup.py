"""消息身份标识与增量去重（水位线模型）。

## 为什么需要这个模块

桌面 OCR 拿不到微信的真实消息 ID。原实现用
``chat|sender|text|timestamp`` 四元组哈希做去重，存在两类确定性误判：

1. **假重复（真消息被吞）**：同一个人连发两条完全相同的内容（如「好的」），
   两条哈希相同，第二条被当作已读而永久丢失。
2. **假新增（同一条重复上报）**：同一屏消息在两次轮询之间，若时间戳识别结果
   出现波动（时有时无），哈希随之变化，导致同一条消息被反复上报。

## 本模块的做法

引入两个概念：

- **weak fingerprint**：仍用四元组哈希，但只作为「相似」判据，不单独决定是否已读。
- **occurrence**：同一可见窗口内，相同 weak fingerprint 从早到晚的第 n 次出现。
  ``identity = weak + ":" + occurrence``，这是本条消息在本窗口内的稳定伪 ID。

配合**水位线（watermark）**：每个会话持久化一条记录，标记「上次已读到哪条」，
由该条的 identity 构成。下一轮轮询时：

1. 在本次窗口里定位水位线对应的那条消息；
2. 它之后（更晚）的消息才是新增；
3. 水位线滚出可见区、无法定位时，返回 ``watermark_lost=True``，
   把整窗视为候选并明确告知可能重复/漏读 —— 不假装连续。

这套机制按**位置**切分增量，而不是只按内容，因此同人连发相同文本时
两条的 identity 不同（occurrence 不同），可以各自上报。

## 已知边界

- occurrence 只在单次窗口内有效。若两次轮询之间旧消息被滚出窗口，
  水位线可能定位失败，此时进入 lost 分支，属于**显式降级**而非静默出错。
- 会话名被修改时，水位线按新名字重建，表现为一次基线重建。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

# ---------------------------------------------------------------- 基础哈希

def _norm(text: str) -> str:
    """压缩空白：OCR 对空格与换行的还原不稳定，比对前必须归一。"""
    return " ".join((text or "").split())


def weak_fingerprint(chat: str, msg: dict, *, with_ts: bool = True) -> str:
    """四元组哈希：会话名 + 发送者 + 正文 + 时间戳。

    这是「相似」判据，不单独作为已读依据。
    ``with_ts=False`` 时剔除时间戳，得到一个抗 OCR 波动的粗指纹。

    之所以需要两个版本：微信的消息时间戳是浅灰小字，Vision OCR 经常整屏漏检
    （实测「12:30」一次未识别）。若只保留含时间戳的单一指纹，
    一旦某轮漏检，所有消息的指纹同时改变，降级匹配也无从谈起。
    """
    parts = [
        _norm(chat),
        _norm(str(msg.get("sender") or "")),
        _norm(str(msg.get("text") or "")),
    ]
    if with_ts:
        parts.append(_norm(str(msg.get("timestamp") or "")))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


@dataclass
class Marked:
    """带身份标识的消息。"""
    identity: str
    weak: str
    weak_no_ts: str
    occurrence: int
    payload: dict


def mark(chat: str, messages: Sequence[dict]) -> List[Marked]:
    """为窗口内消息按时间升序分配 occurrence 与 identity。

    ``messages`` 必须按**从早到晚**排序，否则 occurrence 的语义会反。

    occurrence 按 ``weak_no_ts`` 计数，这样时间戳识别波动不会打乱同一内容的
    第 n 次出现的编号 —— 降级匹配依赖这个性质。
    """
    counts: Dict[str, int] = {}
    out: List[Marked] = []
    for m in messages:
        weak = weak_fingerprint(chat, m, with_ts=True)
        weak_no_ts = weak_fingerprint(chat, m, with_ts=False)
        occ = counts.get(weak_no_ts, 0)
        counts[weak_no_ts] = occ + 1
        out.append(
            Marked(
                identity=f"{weak}:{occ}",
                weak=weak,
                weak_no_ts=weak_no_ts,
                occurrence=occ,
                payload=m,
            )
        )
    return out


# ---------------------------------------------------------------- 水位线存储

@dataclass
class Watermark:
    """某会话的已读水位。"""
    identity: str
    weak: str
    weak_no_ts: str
    occurrence: int
    text: str = ""
    sender: str = ""
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "identity": self.identity,
            "weak": self.weak,
            "weak_no_ts": self.weak_no_ts,
            "occurrence": self.occurrence,
            "text": self.text,
            "sender": self.sender,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Watermark":
        return cls(
            identity=str(d.get("identity", "")),
            weak=str(d.get("weak", "")),
            weak_no_ts=str(d.get("weak_no_ts", d.get("weak", ""))),
            occurrence=int(d.get("occurrence", 0)),
            text=str(d.get("text", "")),
            sender=str(d.get("sender", "")),
            updated_at=float(d.get("updated_at", 0.0)),
        )


class WatermarkStore:
    """水位线的 JSON 持久化。

    单个文件保存所有会话的水位，结构 ``{chat_name: {...}}``。
    超过 ``max_chats`` 时按最后更新时间淘汰最旧条目。
    """

    def __init__(self, path: str, max_chats: int = 200):
        self.path = path
        self.max_chats = max_chats
        self._data: Dict[str, Watermark] = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError):
            self._data = {}
            return
        data: Dict[str, Watermark] = {}
        for chat, d in raw.items():
            if isinstance(d, dict):
                data[chat] = Watermark.from_dict(d)
        self._data = data

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if len(self._data) > self.max_chats:
            keep = sorted(
                self._data.items(), key=lambda kv: kv[1].updated_at, reverse=True
            )[: self.max_chats]
            self._data = dict(keep)
        tmp = self.path + ".tmp"
        payload = {k: v.to_dict() for k, v in self._data.items()}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=0)
        os.replace(tmp, self.path)

    def get(self, chat: str) -> Optional[Watermark]:
        return self._data.get(chat)

    def put(self, chat: str, wm: Watermark) -> None:
        wm.updated_at = time.time()
        self._data[chat] = wm

    def __len__(self) -> int:
        return len(self._data)


# ---------------------------------------------------------------- 增量判定

@dataclass
class Delta:
    """一次增量判定的结果。"""
    fresh: List[Marked] = field(default_factory=list)
    baseline: bool = False          # 该会话首次建立水位线，fresh 实为全窗历史
    watermark_lost: bool = False    # 水位线滚出可见区，无法精确定位
    reused_weak: bool = False       # 定位发生降级（L2/L3）
    matched_level: str = ""         # L1 / L2 / L3 / lost / baseline
    reason: str = ""

    @property
    def messages(self) -> List[dict]:
        return [m.payload for m in self.fresh]


def advance(
    chat: str,
    marked: Sequence[Marked],
    store: WatermarkStore,
    *,
    commit: bool = True,
) -> Delta:
    """与水位线比对，算出新增消息，并推进水位线。

    ``marked`` 必须是本次窗口内**按时间升序**的消息。
    ``commit=False`` 时只计算不落库（供 dry-run 使用）。
    """
    if not marked:
        return Delta(reason="窗口内无可解析消息，水位线保持不变")

    old = store.get(chat)
    if old is None:
        if commit:
            store.put(chat, _wm_from(marked[-1]))
        return Delta(
            fresh=list(marked),
            baseline=True,
            matched_level="baseline",
            reason="首次观测，建立基线",
        )

    # 三级降级定位水位线：
    #   L1 identity 精确命中（内容与时间戳完全一致）
    #   L2 weak 命中（时间戳一致，但 occurrence 编号漂移）
    #   L3 weak_no_ts 命中（时间戳被 OCR 漏检，退到不含时间的粗指纹）
    idx = next(
        (i for i, m in enumerate(marked) if m.identity == old.identity),
        None,
    )
    level = "L1"
    if idx is None:
        key = old.weak_no_ts or old.weak
        cands = [
            (abs(m.occurrence - old.occurrence), m.occurrence, i)
            for i, m in enumerate(marked)
            if (m.weak == old.weak) or (key and m.weak_no_ts == key)
        ]
        if cands:
            cands.sort()
            idx = cands[0][2]
            level = "L2" if marked[idx].weak == old.weak else "L3"

    if idx is None:
        # 水位线滚出可见区：整窗都是候选，但无法确认哪些真的没上报过
        if commit:
            store.put(chat, _wm_from(marked[-1]))
        return Delta(
            fresh=list(marked),
            watermark_lost=True,
            matched_level="lost",
            reason=(
                "上轮已读位置已滚出可见区，无法精确定位水位线；"
                "本次返回整窗消息，其中可能包含已上报过的条目，请按 identity 二次去重"
            ),
        )

    fresh = list(marked[idx + 1:])
    if commit:
        store.put(chat, _wm_from(marked[-1]))
    return Delta(
        fresh=fresh,
        reused_weak=level != "L1",
        matched_level=level,
        reason="ok" if fresh else "无新增",
    )


def _wm_from(m: Marked) -> Watermark:
    return Watermark(
        identity=m.identity,
        weak=m.weak,
        weak_no_ts=m.weak_no_ts,
        occurrence=m.occurrence,
        text=str(m.payload.get("text") or "")[:60],
        sender=str(m.payload.get("sender") or ""),
    )


# ---------------------------------------------------------------- 兼容旧数据

def load_legacy_fingerprints(seen_path: str) -> set:
    """读取旧版 ``seen_messages.json`` 中的指纹集合。

    旧文件是 ``{fingerprint: timestamp}`` 的扁平字典，没有会话维度和顺序信息，
    无法据此还原逐会话水位线。因此它只作为**基线抑制集**使用：
    首次为某会话建立水位线时，把历史指纹命中的条目从 fresh 中剔除，
    避免升级后第一轮把整窗历史当新消息上报。
    """
    try:
        with open(seen_path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(raw, dict):
        return set()
    return {str(k) for k in raw.keys()}
