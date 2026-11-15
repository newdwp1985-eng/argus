"""统一微信工具接口层：9 个工具，统一 JSON 返回。

安全约束（对应任务书第六部分）：
- 联系人无法唯一确认时禁止发送
- 发送必须显式授权（confirm=True）
- 空消息、超长消息、重复内容拒绝发送
- 所有动作写入操作日志
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any, Optional

from .config import cfg_int, describe as describe_config
from .core import core as get_core
from .dedup import (
    WatermarkStore,
    advance,
    load_legacy_fingerprints,
    mark,
)
from .errors import Err, from_state
from .lock import serialized
from .log import get_logger, log_action
from .result import ToolResult, fail, new_operation_id, ok

logger = get_logger("argus.tools")

# 运行时数据（水位线、已读基线）与源码分离：打包安装后仍可写，且升级不丢状态。
DATA_DIR = os.environ.get("ARGUS_DATA_DIR") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "data")
)
WATERMARK_FILE = os.path.join(DATA_DIR, "watermarks.json")
SEEN_FILE = os.path.join(DATA_DIR, "seen_messages.json")  # 旧版数据，仅用于基线抑制
MAX_TEXT_LEN = cfg_int("limits.max_text_len", 2000)
DUPLICATE_WINDOW_SECONDS = cfg_int("limits.duplicate_window_seconds", 30)

# 最近发送记录：{(contact, text_hash): ts}
_recent_sends: dict[tuple[str, str], float] = {}

_store: Optional[WatermarkStore] = None


def _watermarks() -> WatermarkStore:
    global _store
    if _store is None:
        _store = WatermarkStore(WATERMARK_FILE)
    return _store


# ---------------------------------------------------------------- 工具 1

@serialized()
def get_wechat_status() -> ToolResult:
    """检查微信运行状态与当前 UI 是否可用于自动化。"""
    op = new_operation_id()
    c = get_core()
    running = c.is_wechat_running()
    if not running:
        log_action(op, "get_wechat_status", running=False)
        return fail("微信未运行，请先启动并登录 Mac 微信", Err.WECHAT_NOT_RUNNING, op)

    result, state = c.perceive()
    data = {
        "wechat_running": True,
        "frontmost": c.frontmost_is_wechat(),
        "ui_status": state.status,
        "ui_detail": state.detail,
        "window": (
            {
                "x": state.window_rect.x,
                "y": state.window_rect.y,
                "width": state.window_rect.width,
                "height": state.window_rect.height,
            }
            if state.window_rect is not None
            else None
        ),
        "click_backend": "cliclick" if not c.clicker.degraded else "cgevent(fallback)",
        "current_chat": getattr(result, "chat_name", None) if result else None,
    }
    log_action(op, "get_wechat_status", ui=state.status, window=data["window"])

    if not state.usable:
        return ToolResult(
            success=False,
            operation_id=op,
            data=data,
            error=state.detail or f"当前界面不可用: {state.status}",
            error_code=from_state(state.status),
        )
    return ok(data, op)


# ---------------------------------------------------------------- 工具 2

@serialized()
def get_chat_list() -> ToolResult:
    """获取左侧会话列表。"""
    op = new_operation_id()
    c = get_core()
    result, state = c.perceive()
    if not state.usable:
        log_action(op, "get_chat_list", ui=state.status)
        return fail(state.detail or f"界面不可用: {state.status}", from_state(state.status), op)

    items = [
        {
            "nickname": it.nickname,
            "preview": it.last_message_preview,
            "unread": it.unread_count,
            "timestamp": it.timestamp,
            "rect": {"x": it.rect.x, "y": it.rect.y, "w": it.rect.width, "h": it.rect.height},
        }
        for it in result.chat_list_items
    ]
    log_action(op, "get_chat_list", count=len(items))
    return ok({"count": len(items), "items": items}, op)


# ---------------------------------------------------------------- 联系人定位

def _normalize(s: str) -> str:
    """归一化昵称：去掉各类省略号与尾部残留句点。

    微信列表的截断形式实测是「某某物流：天⋯.」——省略号后面还跟一个句点。
    只删省略号会留下那个点，使「按前缀匹配」永远失败，因此末尾统一 rstrip。
    """
    t = (s or "").strip().replace("⋯", "").replace("...", "").replace("…", "")
    return t.rstrip(".").strip()


def _match_contact(name: str, items) -> tuple[Any, list[str]]:
    """在会话列表中匹配联系人。

    返回 (唯一命中项 or None, 候选名列表)。
    微信会话列表的昵称常被 UI 截断（如「某某物流：天⋯.」），因此按前缀与包含关系双重匹配。
    """
    target = _normalize(name)
    if not target:
        return None, []

    exact = [it for it in items if _normalize(it.nickname) == target]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, [it.nickname for it in exact]

    # 列表昵称是目标名的截断形式：item 去掉省略号后是 target 的前缀
    prefix = [it for it in items if target.startswith(_normalize(it.nickname)) and _normalize(it.nickname)]
    if len(prefix) == 1:
        return prefix[0], []
    if len(prefix) > 1:
        return None, [it.nickname for it in prefix]

    contains = [it for it in items if target in _normalize(it.nickname)]
    if len(contains) == 1:
        return contains[0], []
    if len(contains) > 1:
        return None, [it.nickname for it in contains]

    return None, []


def _open_contact(name: str, max_scroll: int = 8) -> tuple[bool, str, list[str], str]:
    """打开指定联系人的聊天窗口。

    返回 (ok, 当前聊天名, 候选列表, 错误信息)。
    """
    c = get_core()
    for attempt in range(max_scroll + 1):
        result, state = c.perceive()
        if not state.usable:
            return False, "", [], state.detail or state.status

        # 优先校验「当前已打开的聊天」是否就是目标。
        # 群聊常从通知点开，此时会话列表可能已滚到别处，若只按列表匹配会误判为找不到。
        # 以窗口内聊天标题为准，直接匹配则不再做任何点击，避免多余误点。
        cur_norm = _normalize(result.chat_name)
        tgt_norm = _normalize(name)
        if cur_norm and tgt_norm and (tgt_norm in cur_norm or cur_norm in tgt_norm):
            return True, result.chat_name, [], ""

        items = result.chat_list_items
        hit, candidates = _match_contact(name, items)

        if candidates:
            return False, result.chat_name, candidates, "联系人匹配到多个会话，需人工确认"

        if hit is not None:
            # 已打开则直接返回
            if _normalize(name) in _normalize(result.chat_name) or _normalize(result.chat_name) in _normalize(name):
                return True, result.chat_name, [], ""

            cx = hit.rect.x + hit.rect.width / 2.0
            cy = hit.rect.y + hit.rect.height / 2.0
            clicked, err = c.click_screenshot_point(cx, cy)
            if not clicked:
                return False, result.chat_name, [], f"点击会话失败: {err}"
            time.sleep(1.1)

            after, state2 = c.perceive()
            if not state2.usable:
                return False, "", [], f"切换后界面不可用: {state2.detail}"
            new_chat = after.chat_name
            if _normalize(name) in _normalize(new_chat) or _normalize(new_chat) in _normalize(name):
                return True, new_chat, [], ""
            return False, new_chat, [new_chat], f"点击后聊天标题为「{new_chat}」，与目标「{name}」不一致"

        # 当前屏未命中：滚动会话列表继续找
        if attempt < max_scroll and items:
            c.scroll(items[0].rect.x + 40, items[len(items) // 2].rect.y, -8)
            time.sleep(0.4)

    return False, "", [], f"在会话列表中没有找到「{name}」，可能不在最近会话中"


# ---------------------------------------------------------------- 工具 3

@serialized()
def search_contact(name: str) -> ToolResult:
    """搜索联系人：仅做列表匹配，不切换、不发送。"""
    op = new_operation_id()
    c = get_core()
    result, state = c.perceive()
    if not state.usable:
        log_action(op, "search_contact", ui=state.status, query=name)
        return fail(state.detail or f"界面不可用: {state.status}", from_state(state.status), op)

    hit, candidates = _match_contact(name, result.chat_list_items)
    if candidates:
        log_action(op, "search_contact", query=name, ambiguous=len(candidates))
        return ToolResult(
            success=False,
            operation_id=op,
            data={"query": name, "candidates": candidates},
            error="匹配到多个会话，请提供更完整的名称",
            error_code=Err.AMBIGUOUS,
        )
    if hit is None:
        log_action(op, "search_contact", query=name, found=False)
        return ToolResult(
            success=False,
            operation_id=op,
            data={
                "query": name,
                "candidates": [it.nickname for it in result.chat_list_items],
            },
            error=f"未在会话列表中找到「{name}」",
            error_code=Err.NOT_FOUND,
        )

    log_action(op, "search_contact", query=name, found=True)
    return ok(
        {
            "query": name,
            "nickname": hit.nickname,
            "unread": hit.unread_count,
            "preview": hit.last_message_preview,
            "rect": {"x": hit.rect.x, "y": hit.rect.y, "w": hit.rect.width, "h": hit.rect.height},
        },
        op,
    )


# ---------------------------------------------------------------- 消息序列化

def _msg_to_dict(m) -> dict:
    return {
        "sender": m.sender,
        "sender_type": str(getattr(m, "sender_type", "")).split(".")[-1],
        "text": m.text,
        "timestamp": m.timestamp,
        "message_type": getattr(m, "message_type", "text"),
    }


def _fingerprint(chat: str, msg: dict) -> str:
    """兼容旧接口：返回四元组粗指纹。

    新代码请使用 ``_mark_messages`` 得到的 ``message_id``，
    它额外带 occurrence 编号，可区分同人连发的相同内容。
    """
    from .dedup import weak_fingerprint

    return weak_fingerprint(chat, msg)


def _mark_messages(chat: str, dicts: list[dict]) -> list[dict]:
    """为消息附加稳定身份标识。

    输出的每条消息带三个字段：
      · ``message_id``   伪消息 ID = 四元组哈希 + 会话内出现序号
      · ``occurrence``   同一窗口内相同内容的第 n 次出现（0 起）
      · ``fingerprint``  仅四元组哈希，保留以兼容旧调用方

    微信桌面版没有可读的真实消息 ID，这个 ID 是本系统能给出的最强身份：
    同一屏内容重复读取时保持不变；同一个人连发两条相同文本时，两条 ID 不同。
    """
    return [
        {
            **m.payload,
            "message_id": m.identity,
            "occurrence": m.occurrence,
            "fingerprint": m.weak,
        }
        for m in mark(chat, dicts)
    ]


# ---------------------------------------------------------------- 工具 4

def _deep_history(op, c, contact: str, chat_name: str, limit: int, max_scrolls: int) -> ToolResult:
    """向上滚动回溯并拼接多屏历史。

    滚动依赖滚轮事件，而滚轮事件只会投递给最前面的窗口 —— 微信在后台时
    事件会被前台应用吃掉。因此这里把「能否置前」作为硬前置条件，
    不能置前就明确失败，不做无效重试。
    """
    from .paginate import restore_bottom, scroll_history

    if not c.ensure_frontmost(max_wait=12):
        log_action(op, "get_chat_history.deep", contact=contact, ok=False, err="not_frontmost")
        return ToolResult(
            success=False,
            operation_id=op,
            data={"contact": contact, "chat_name": chat_name, "requires": "wechat_frontmost"},
            error=(
                "滚动回溯需要微信处于前台（滚轮事件只投递给最前面的窗口），"
                "当前无法将微信置前，可能你正在操作其他应用。"
                "请手动切换到微信窗口后重试，或使用 deep=False 只读当前可见区。"
            ),
            error_code=Err.WECHAT_NOT_FRONTMOST,
        )

    page = None
    cursor = c.cursor_pos()
    try:
        page = scroll_history(c, max_scrolls=max_scrolls, target_count=limit or 200)
    finally:
        # 成功与否都要滚回最新位置，不能把用户界面留在历史中间；
        # 同时把光标移回原处 —— 滚动需要真实光标停在聊天区，这是副作用
        try:
            restore_bottom(c)
        except Exception as exc:  # noqa: BLE001
            logger.warning("回溯后归位失败: %s", exc)
        try:
            c.move_cursor(*cursor)
        except Exception as exc:  # noqa: BLE001
            logger.warning("光标复原失败: %s", exc)

    if page is None or not page.messages:
        return fail("回溯未读到任何消息", Err.EMPTY_RESULT, op)

    msgs = _mark_messages(chat_name, page.messages)
    if limit and len(msgs) > limit:
        msgs = msgs[-limit:]
        truncated = True
    else:
        truncated = False

    warnings = list(page.notes)
    if page.gaps:
        warnings.append(
            f"检测到 {len(page.gaps)} 处锚点缺口，缺口位置的消息可能缺失，"
            "本结果不可视为完整历史"
        )
    if page.direction == 0:
        warnings.append("本次未发生实际滚动，返回值等同于可见区内容")
    if truncated:
        warnings.append(f"结果已按 limit={limit} 截断，只保留最近的 {limit} 条")

    log_action(
        op,
        "get_chat_history.deep",
        contact=contact,
        chat=chat_name,
        count=len(msgs),
        pages=page.pages_read,
        scrolls=page.scrolls,
        gaps=len(page.gaps),
        top=page.reached_top,
    )
    return ok(
        {
            "contact": contact,
            "chat_name": chat_name or page.chat_name,
            "count": len(msgs),
            "messages": msgs,
            "deep": True,
            "pages_read": page.pages_read,
            "scrolls": page.scrolls,
            "reached_top": page.reached_top,
            "gap_count": len(page.gaps),
            "gaps": page.gaps,
            "truncated": truncated,
            "note": (
                "本结果由多屏截图滚动拼接而成，锚点为（发送者+正文前 20 字）。"
                "无文本消息（表情、图片、红包）不参与拼接，因此条数可能少于肉眼所见。"
                if not page.reached_top
                else "已回溯至会话开头（连续两次滚动无位移）。"
            ),
        },
        op,
        warnings=warnings,
    )


@serialized()
def get_chat_history(
    contact: str,
    limit: int = 30,
    deep: bool = False,
    max_scrolls: int = 10,
) -> ToolResult:
    """读取指定联系人的聊天记录（会切换到该会话）。

    ``deep=False`` 只读当前可见区（单屏，快，不需要前台）。
    ``deep=True`` 向上滚动回溯，拼接多屏历史。滚动属于输入操作，必须微信在前台，
    因此会先尝试激活；激活不成功时返回 WECHAT_NOT_FRONTMOST，不做无效尝试。
    """
    op = new_operation_id()
    ok_open, chat_name, candidates, err = _open_contact(contact)
    if not ok_open:
        log_action(op, "get_chat_history", contact=contact, ok=False, err=err, candidates=candidates)
        return ToolResult(
            success=False,
            operation_id=op,
            data={"contact": contact, "candidates": candidates},
            error=err,
            error_code=Err.AMBIGUOUS if candidates else Err.CONTACT_NOT_FOUND,
        )

    c = get_core()

    if deep:
        return _deep_history(op, c, contact, chat_name, limit, max_scrolls)

    result, state = c.perceive()
    if not state.usable:
        return fail(state.detail or state.status, from_state(state.status), op)

    msgs = _mark_messages(chat_name, [_msg_to_dict(m) for m in result.messages])
    msgs = msgs[-limit:] if limit and len(msgs) > limit else msgs

    log_action(op, "get_chat_history", contact=contact, chat=chat_name, count=len(msgs))
    return ok(
        {
            "contact": contact,
            "chat_name": chat_name,
            "is_group": getattr(result, "is_group", False),
            "count": len(msgs),
            "messages": msgs,
            "note": "仅返回当前可见区域内的消息；更早的历史需滚动加载，本版本未做跨屏回溯",
        },
        op,
    )


# ---------------------------------------------------------------- 工具 5

@serialized()
def get_latest_messages(limit: int = 10) -> ToolResult:
    """读取当前聊天窗口的最新消息，不切换会话。"""
    op = new_operation_id()
    c = get_core()
    result, state = c.perceive()
    if not state.usable:
        log_action(op, "get_latest_messages", ui=state.status)
        return fail(state.detail or state.status, from_state(state.status), op)

    msgs = _mark_messages(result.chat_name, [_msg_to_dict(m) for m in result.messages])
    msgs = msgs[-limit:] if limit else msgs

    log_action(op, "get_latest_messages", chat=result.chat_name, count=len(msgs))
    return ok({"chat_name": result.chat_name, "count": len(msgs), "messages": msgs}, op)


# ---------------------------------------------------------------- 工具 6

@serialized()
def monitor_new_messages(
    duration_seconds: int = 30, interval: float = 3.0, once: bool = False
) -> ToolResult:
    """轮询监听当前聊天的新消息。

    去重采用水位线模型而非纯哈希比对：
      · 每条消息带 message_id（四元组哈希 + 窗口内出现序号），
        同一个人连发两条相同文本时两条 ID 不同，不会被互相吞掉；
      · 每个会话持久化一条水位线，下轮从窗口内定位水位线，其后的才是新增；
      · 水位线滚出可见区时标记 watermark_lost，返回整窗候选并提示二次去重。

    once=True（默认）只做一轮感知就返回，节奏由调用方掌握；once=False 则在
    duration_seconds 秒内持续轮询——调用会阻塞到窗口结束，且期间持有全局闸门，
    其他工具会拿到 BUSY。两种模式共用同一份水位线，可以混用。

    注意：这是轮询实现，不是推送。轮询间隔内的短暂消息若被界面刷掉仍会漏读，
    返回值中同时给出 poll_count 与 uncertainty_note，不将轮询成功等同于实时送达。
    """
    op = new_operation_id()
    c = get_core()
    store = _watermarks()
    # 旧版扁平指纹集：仅用于首次建立基线时抑制历史条目，避免升级后把整窗历史当新消息
    legacy = load_legacy_fingerprints(SEEN_FILE)

    cap = cfg_int("limits.monitor_max_seconds", 600)
    duration_seconds = min(max(1, int(duration_seconds)), cap)
    max_rounds = 1 if once else None

    collected: list[dict] = []
    reported: set[str] = set()
    levels: set[str] = set()
    polls = 0
    lost_events = 0
    started = time.time()
    deadline = started + duration_seconds
    last_chat = ""

    while True:
        result, state = c.perceive()
        polls += 1
        if state.usable:
            last_chat = result.chat_name or last_chat
            marked = mark(last_chat, [_msg_to_dict(m) for m in result.messages])
            delta = advance(last_chat, marked, store, commit=True)
            levels.add(delta.matched_level)
            if delta.watermark_lost:
                lost_events += 1

            for item in delta.fresh:
                if item.identity in reported:
                    continue
                if delta.baseline and item.weak in legacy:
                    continue
                reported.add(item.identity)
                collected.append(
                    {
                        **item.payload,
                        "message_id": item.identity,
                        "occurrence": item.occurrence,
                        "fingerprint": item.weak,
                        "chat_name": last_chat,
                        "poll_index": polls,
                    }
                )
        if max_rounds is not None and polls >= max_rounds:
            break
        if time.time() >= deadline:
            break
        time.sleep(max(0.2, interval))

    elapsed = round(time.time() - started, 2)
    store.save()
    log_action(
        op,
        "monitor_new_messages",
        polls=polls,
        new=len(collected),
        chat=last_chat,
        lost=lost_events,
    )

    notes = []
    if "baseline" in levels:
        notes.append(
            "本次为首次监听该会话，已把当前可见消息记为基线；"
            "new_messages 可能含历史消息（旧版已记录过的条目已剔除）。"
        )
    if lost_events:
        notes.append(
            f"有 {lost_events} 次轮询未能定位上次已读位置（旧消息被挤出可见区），"
            "这些轮次的候选已按 message_id 去重，但仍可能残留重复。"
        )
    notes.append(
        "本轮为轮询式监听，非实时推送。若消息在两次轮询之间被界面滚动挤出可见区，可能漏读。"
        "message_id 由（会话名+发送者+正文+时间戳）哈希加窗口内出现序号构成，"
        "不是微信真实消息 ID：同一屏内可区分连发的相同内容，"
        "但跨屏且内容与时间戳完全相同的两条消息仍存在碰撞可能。"
    )

    return ok(
        {
            "chat_name": last_chat,
            "mode": "once" if once else "window",
            "poll_count": polls,
            "elapsed_seconds": elapsed,
            "interval_seconds": interval,
            "new_count": len(collected),
            "new_messages": collected,
            "watermark_lost_events": lost_events,
            "uncertainty_note": "".join(notes),
        },
        op,
    )


# ---------------------------------------------------------------- 工具 7

@serialized()
def send_message(contact: str, text: str, confirm: bool = False, wait_frontmost: float = 15.0) -> ToolResult:
    """向指定联系人发送文字消息。

    confirm 必须为 True（人工授权），否则拒绝执行。
    wait_frontmost 为等待窗口置于前台的最长秒数。

    前置条件：微信必须在前台。实测 macOS 26 拒绝对后台进程的焦点接管，
    三种激活 API 均返回成功但实际无效，因此这里不做强行切换，
    而是明确要求人工把微信切到前台——这比把消息粘贴到别的应用安全得多。
    """
    op = new_operation_id()

    if not confirm:
        log_action(op, "send_message", contact=contact, blocked="no_confirm")
        return fail("发送未授权：需显式传入 confirm=True 表示已人工确认", Err.NOT_CONFIRMED, op)

    text = (text or "").strip()
    if not text:
        return fail("消息内容为空，已拒绝发送", Err.EMPTY_MESSAGE, op)
    if len(text) > MAX_TEXT_LEN:
        return fail(f"消息长度 {len(text)} 超过上限 {MAX_TEXT_LEN}", Err.MESSAGE_TOO_LONG, op)

    key = (contact, hashlib.sha256(text.encode()).hexdigest()[:12])
    now = time.time()
    if key in _recent_sends and now - _recent_sends[key] < DUPLICATE_WINDOW_SECONDS:
        log_action(op, "send_message", contact=contact, blocked="duplicate")
        return fail(
            f"{DUPLICATE_WINDOW_SECONDS} 秒内向同一联系人发送了相同内容，已阻止重复发送",
            Err.DUPLICATE_BLOCKED,
            op,
        )

    c0 = get_core()
    if not c0.ensure_frontmost(max_wait=wait_frontmost):
        log_action(op, "send_message", contact=contact, blocked="not_frontmost")
        return fail(
            "微信不在前台，已拒绝发送（避免消息被粘贴到其他应用）。"
            "请把微信窗口切到前台后重试。",
            Err.WECHAT_NOT_FRONTMOST,
            op,
        )

    ok_open, chat_name, candidates, err = _open_contact(contact)
    if not ok_open:
        log_action(op, "send_message", contact=contact, ok=False, err=err)
        return ToolResult(
            success=False,
            operation_id=op,
            data={"contact": contact, "candidates": candidates},
            error=f"未发送：{err}",
            error_code=Err.CONTACT_NOT_CONFIRMED,
        )

    c = get_core()
    result, state = c.perceive()
    if not state.usable:
        return fail(f"未发送：{state.detail or state.status}", from_state(state.status), op)
    if state.window_rect is None:
        return fail("未发送：无法获取窗口位置", Err.NO_WINDOW, op)

    # 统一走可信发送路径：清空 → 粘贴 → 回读输入框 → 点击「发送」按钮 → 复核已清空。
    # 这里曾直接投递回车键（key code 36），但微信的回车键行为取决于「按 Enter 键发送消息」
    # 设置：该设置关闭时回车只插入换行。实测因此把两条消息留在了输入框里，
    # 而旧代码不论是否确认都返回成功 —— 属于误报，已改为点击按钮 + 复核。
    sent_ok, serr = c.send_text_verified(text)
    if not sent_ok:
        log_action(op, "send_message", contact=contact, ok=False, err=serr)
        return fail(f"未发送：{serr}", Err.SEND_UNVERIFIED, op)

    _recent_sends[key] = time.time()
    time.sleep(1.0)

    # 二次确认：从消息区读到刚发的内容。读不到不代表没发出（可能被新消息挤出可见区），
    # 因此不作失败处理，但必须显式告知调用方「未能在界面上确认」。
    after, state2 = c.perceive()
    tail = c._core_probe(text, 8)
    confirmed = bool(
        state2.usable
        and after is not None
        and any(
            tail and tail in " ".join((m.text or "").split())
            for m in after.messages[-4:]
        )
    )

    warnings = []
    if not confirmed:
        warnings.append(
            "发送动作已通过输入框清空校验，但未能在消息区读到该内容，"
            "可能是被后续消息挤出可见区，也可能未真正上屏 —— 请人工核对"
        )

    log_action(
        op,
        "send_message",
        contact=contact,
        chat=chat_name,
        length=len(text),
        confirmed_in_ui=confirmed,
    )
    return ok(
        {
            "contact": contact,
            "chat_name": chat_name,
            "text": text,
            "sent": True,
            "confirmed_in_ui": confirmed,
            "note": (
                "已点击「发送」并通过输入框清空校验。"
                "confirmed_in_ui=True 表示发送后截图在最近消息中读到了该内容；"
                "False 表示未能从界面确认，请人工核对。"
            ),
        },
        op,
        warnings=warnings,
    )


# ---------------------------------------------------------------- 工具 8

@serialized()
def get_screenshot() -> ToolResult:
    """获取微信窗口截图并保存到工具目录。"""
    op = new_operation_id()
    c = get_core()
    result, state = c.perceive()
    if state.window_rect is None and result is None:
        return fail(state.detail or state.status, from_state(state.status), op)

    src = (result.screenshot_path if result else None) or c._last_image  # noqa: SLF001
    if not src or not os.path.exists(src):
        return fail("截图文件不存在", Err.NO_SCREENSHOT, op)

    dst_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "data", "shots"))
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, f"wechat_{int(time.time())}.png")
    with open(src, "rb") as fh:
        payload = fh.read()
    with open(dst, "wb") as fh:
        fh.write(payload)

    log_action(op, "get_screenshot", path=dst)
    return ok({"path": dst, "bytes": len(payload)}, op)


# ---------------------------------------------------------------- 工具 9

@serialized()
def get_ocr_result() -> ToolResult:
    """返回当前窗口的原始 OCR 元素（用于排障与布局校准）。"""
    op = new_operation_id()
    c = get_core()
    result, state = c.perceive()
    if not state.usable:
        return fail(state.detail or state.status, from_state(state.status), op)

    elements = c.ocr.recognize(result.screenshot_path)
    data = [
        {
            "text": e.text,
            "x": e.center.x,
            "y": e.center.y,
            "w": e.bbox.width,
            "h": e.bbox.height,
            "confidence": round(float(e.confidence), 3),
        }
        for e in elements
    ]
    log_action(op, "get_ocr_result", count=len(data))
    return ok({"count": len(data), "elements": data}, op)


TOOLS = {
    "get_wechat_status": get_wechat_status,
    "get_chat_list": get_chat_list,
    "search_contact": search_contact,
    "get_chat_history": get_chat_history,
    "get_latest_messages": get_latest_messages,
    "monitor_new_messages": monitor_new_messages,
    "send_message": send_message,
    "get_screenshot": get_screenshot,
    "get_ocr_result": get_ocr_result,
}
