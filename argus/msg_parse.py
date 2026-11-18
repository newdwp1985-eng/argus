"""群聊消息结构化解析。

原项目的 extractor 用「x 坐标带」判断昵称，在窄窗口与 4.x 三栏布局下失效，
导致群成员一律被记为「对方」，时间戳也丢失。

本模块改用像素证据重新归属：
  · 群成员昵称  墨色约 (148,148,156)，亮度 ~149
  · 消息正文    墨色约 (25,25,26)，亮度 ~25
  · 气泡内引用  与昵称同色，但以冒号结尾，可据此排除
  · 时间戳      居中显示，形态固定（HH:MM / 昨天 HH:MM / 星期X HH:MM）

所有阈值均来自本机 macOS 26 + 微信 4.1.15 的实测取样。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

from argus.vendor.models import ChatMessage, OCRTextElement, Rect, SenderType

from .config import cfg_float
from .errors import EnvUnavailable, Err

# numpy 只在真正做像素统计时被用到。这里允许缺失，是为了让本模块的
# 「结构化 / 时间戳判定」等纯逻辑部分在没有科学计算库的环境里仍可导入、
# 仍可被单元测试覆盖；一旦走到墨色分析就会抛出明确的 DEPENDENCY_MISSING。
try:
    import numpy as np
except Exception as _exc:  # noqa: BLE001
    np = None  # type: ignore[assignment]
    _NUMPY_ERROR: Optional[str] = f"{type(_exc).__name__}: {_exc}"
else:
    _NUMPY_ERROR = None


def _require_numpy():
    if np is None:
        raise EnvUnavailable(
            "缺少 numpy，无法做墨色分析；请执行 pip install -r requirements.txt"
            + (f"（{_NUMPY_ERROR}）" if _NUMPY_ERROR else ""),
            Err.DEPENDENCY_MISSING,
        )
    return np

# --- 实测阈值 -------------------------------------------------------------
# 下列常量仅作「配置文件缺失时的兜底默认值」。实际取值来自 profile_default.json，
# 换显示器缩放或改微信字号后调 JSON 即可，不必改源码。
BODY_INK_MAX = 90          # 亮度低于此值判为正文（黑字）
LABEL_INK_MIN = 110        # 亮度高于此值判为灰色标签（昵称/引用）
NICKNAME_MAX_GAP = 90      # 昵称与下方正文的最大垂直间距（像素，2x）
BUBBLE_GAP_RATIO = 1.7     # 同一气泡内行距上限 = 行高中位数 * 该系数
BUBBLE_GAP_MIN = 56        # 同上，绝对下限
NICKNAME_X_TOL = 80        # 昵称与气泡左缘的横向容差


def _thresholds() -> dict:
    """读取当前生效的解析阈值，一次取全，避免逐个重复查配置。"""
    return {
        "body_max": cfg_float("parse.body_ink_max", BODY_INK_MAX),
        "label_min": cfg_float("parse.label_ink_min", LABEL_INK_MIN),
        "nick_gap": cfg_float("parse.nickname_max_gap", NICKNAME_MAX_GAP),
        "gap_ratio": cfg_float("parse.bubble_gap_ratio", BUBBLE_GAP_RATIO),
        "gap_min": cfg_float("parse.bubble_gap_min", BUBBLE_GAP_MIN),
        "nick_tol": cfg_float("parse.nickname_x_tol", NICKNAME_X_TOL),
    }

# 输入框占位提示等非消息文本
PLACEHOLDERS = ("长按鼠标", "语音输入", "按住说话", "发送(S)", "发送")

_TIME_RES = [
    re.compile(r"^\d{1,2}:\d{2}$"),
    re.compile(r"^昨天\s*\d{1,2}:\d{2}$"),
    re.compile(r"^星期[一二三四五六日天]\s*\d{1,2}:\d{2}$"),
    re.compile(r"^(上午|下午|中午|晚上|凌晨)\s*\d{1,2}:\d{2}$"),
    re.compile(r"^\d{1,2}月\d{1,2}日(\s+\d{1,2}:\d{2})?$"),
    re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日"),
]


def _ink_luminance(arr: np.ndarray, bbox: Rect) -> float:
    """取元素包围盒内最暗 5% 像素的平均亮度，代表文字墨色。"""
    np = _require_numpy()
    h, w = arr.shape[:2]
    x0, y0 = max(0, int(bbox.x)), max(0, int(bbox.y))
    x1, y1 = min(w, int(bbox.x + bbox.width)), min(h, int(bbox.y + bbox.height))
    if x1 <= x0 or y1 <= y0:
        return 255.0
    patch = arr[y0:y1, x0:x1].reshape(-1, 3).astype(np.float32)
    lum = patch @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    k = max(1, int(len(lum) * 0.05))
    return float(np.partition(lum, k - 1)[:k].mean())


def _is_timestamp(text: str) -> bool:
    t = text.strip()
    return any(p.match(t) for p in _TIME_RES)


def _is_placeholder(text: str) -> bool:
    return any(p in text for p in PLACEHOLDERS)


@dataclass
class _Item:
    el: OCRTextElement
    ink: float
    kind: str  # "body" | "label" | "time" | "noise"


def _classify(elements: Sequence[OCRTextElement], arr: np.ndarray,
              chat_left: float, chat_top: float, chat_bottom: float,
              image_w: float) -> List[_Item]:
    np = _require_numpy()
    thr = _thresholds()
    items: List[_Item] = []
    for el in elements:
        text = (el.text or "").strip()
        if not text:
            continue
        if el.center.x < chat_left or el.center.y > chat_bottom or el.center.y < chat_top:
            continue
        if _is_placeholder(text):
            items.append(_Item(el, 255.0, "noise"))
            continue
        ink = _ink_luminance(arr, el.bbox)
        # 时间戳：居中 + 形态固定
        center_ratio = el.center.x / image_w
        if _is_timestamp(text) and 0.45 <= center_ratio <= 0.95:
            items.append(_Item(el, ink, "time"))
        elif ink <= thr["body_max"]:
            items.append(_Item(el, ink, "body"))
        elif ink >= thr["label_min"]:
            items.append(_Item(el, ink, "label"))
        else:
            items.append(_Item(el, ink, "body"))
    items.sort(key=lambda i: (i.el.center.y, i.el.center.x))
    return items


def _is_quote_label(text: str) -> bool:
    t = text.strip()
    return t.endswith("：") or t.endswith(":")


def _point_in(rect: Rect, x: float, y: float) -> bool:
    return rect.x <= x <= rect.x + rect.width and rect.y <= y <= rect.y + rect.height


def parse_messages(
    elements: Sequence[OCRTextElement],
    arr: np.ndarray,
    chat_left: float,
    chat_bottom: float,
    chat_name: str,
    self_bubbles: Optional[Sequence[Rect]] = None,
    chat_top: float = 0.0,
) -> List[ChatMessage]:
    """把 OCR 元素还原为带发送者与时间戳的消息列表。"""
    np = _require_numpy()
    thr = _thresholds()
    image_w = float(arr.shape[1])
    items = _classify(elements, arr, chat_left, chat_top, chat_bottom, image_w)
    bodies = [i for i in items if i.kind == "body"]
    if not bodies:
        return []

    heights = [b.el.bbox.height for b in bodies]
    med_h = float(np.median(heights)) if heights else 32.0
    gap_thr = max(med_h * thr["gap_ratio"], thr["gap_min"])

    # 1) 正文按纵向间距聚成气泡
    bubbles: List[List[_Item]] = [[bodies[0]]]
    for it in bodies[1:]:
        if it.el.center.y - bubbles[-1][-1].el.center.y <= gap_thr:
            bubbles[-1].append(it)
        else:
            bubbles.append([it])

    # 2) 昵称候选：灰色、非引用（不以冒号结尾）
    labels = [i for i in items if i.kind == "label" and not _is_quote_label(i.el.text)]
    times = [i for i in items if i.kind == "time"]

    messages: List[ChatMessage] = []
    for group in bubbles:
        group.sort(key=lambda i: i.el.center.y)
        first = group[0]
        text = " ".join(i.el.text.strip() for i in group)
        left = min(i.el.bbox.x for i in group)
        top_y = first.el.center.y

        # 归属：气泡正上方、横向对齐的最近灰色昵称
        sender = "对方"
        best = None
        for lb in labels:
            gap = top_y - lb.el.center.y
            if not (0 < gap <= thr["nick_gap"]):
                continue
            if abs(lb.el.bbox.x - left) > thr["nick_tol"]:
                continue
            if best is None or lb.el.center.y > best.el.center.y:
                best = lb
        if best is not None:
            sender = best.el.text.strip()

        # 时间戳：该气泡上方最近的一条
        ts = None
        for t in times:
            if t.el.center.y < top_y:
                if ts is None or t.el.center.y > ts.el.center.y:
                    ts = t
        timestamp = ts.el.text.strip() if ts is not None else None

        is_self = False
        if self_bubbles:
            cx, cy = first.el.center.x, first.el.center.y
            is_self = any(_point_in(b, cx, cy) for b in self_bubbles)

        messages.append(
            ChatMessage(
                text=text,
                sender="自己" if is_self else sender,
                sender_type=SenderType.SELF if is_self else SenderType.OTHER,
                chat_name=chat_name,
                timestamp=timestamp,
                is_at_me="@" in text,
                source_elements=[i.el for i in group],
            )
        )

    return messages


def recover_timestamps(
    image_path: str,
    chat_left: float,
    chat_top: float,
    chat_bottom: float,
) -> List[tuple]:
    """补充识别漏检的时间戳。

    微信的时间戳是浅灰小字，Vision OCR 在原始截图上经常漏检
    （实测「12:30」一次未识别）。这里对聊天区做对比度增强 + 2 倍放大后
    再识别一次，把坐标还原到原图空间返回 [(y, text)]。

    仅在主识别完全没有拿到时间戳时才调用，避免常态开销。
    """
    import tempfile

    from PIL import Image, ImageOps

    from argus.vendor.vision_ocr import VisionOCREngine

    with Image.open(image_path) as im:
        rgb = im.convert("RGB")
        width, height = rgb.size
        x0 = max(0, int(chat_left))
        y0 = max(0, int(chat_top))
        x1 = width
        y1 = min(height, int(chat_bottom) + 40)
        if x1 <= x0 or y1 <= y0:
            return []
        crop = rgb.crop((x0, y0, x1, y1))

    crop = ImageOps.autocontrast(crop, cutoff=1)
    crop = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        tmp = fh.name
    crop.save(tmp)
    try:
        els = VisionOCREngine().recognize(tmp)
    finally:
        try:
            import os

            os.unlink(tmp)
        except OSError:
            pass

    out = []
    for e in els:
        if not _is_timestamp(e.text):
            continue
        cx = x0 + e.center.x / 2.0
        cy = y0 + e.center.y / 2.0
        if 0.45 <= cx / width <= 0.95:
            out.append((cy, e.text.strip()))
    out.sort()
    return out
