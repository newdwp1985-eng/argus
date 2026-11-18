"""环境自检：不碰屏幕、不碰微信、不依赖第三方库的一组断言。

为什么要单独做一份：审核与 CI 通常跑在「干净机器」上 —— 没有 pyobjc、
没有已登录的微信、也拿不到屏幕录制权限。在这类环境里跑端到端流程必然失败，
但失败并不能说明代码有问题。``argus self-test`` 提供的是另一类证据：

    在这台机器上，凡是「不需要屏幕」的逻辑是否全部正确。

覆盖四块最容易被改坏、且完全可用纯 Python 验证的部分：错误码体系、
配置加载、增量去重、跨屏拼接。任何一项挂了都说明代码真的坏了，
而不是环境不对。结果以 JSON 输出，便于机器判读。
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from typing import Any, Callable, Dict, List

_RESULTS: List[Dict[str, Any]] = []


def _check(name: str, fn: Callable[[], bool]) -> None:
    """执行一项断言，记录结果而不中断后续检查。"""
    try:
        passed = bool(fn())
        detail = "" if passed else "断言返回 False"
    except Exception as exc:  # noqa: BLE001 自检不允许因单项异常整体失败
        passed = False
        detail = f"{type(exc).__name__}: {exc}"
        if "--verbose" in sys.argv:
            detail += "\n" + traceback.format_exc()
    _RESULTS.append({"check": name, "ok": passed, "detail": detail})


def _section(title: str) -> None:
    _RESULTS.append({"check": f"[{title}]", "ok": True, "detail": "", "section": True})


def _msg(text: str, sender: str = "某人", ts: str | None = "10:00") -> dict:
    """构造一条与工具层同构的消息字典（dedup / paginate 都吃这种结构）。"""
    return {"sender": sender, "text": text, "timestamp": ts, "message_id": ""}


def _store():
    from argus.dedup import WatermarkStore

    path = os.path.join(tempfile.mkdtemp(prefix="argus-selftest-"), "watermarks.json")
    return WatermarkStore(path)


def run() -> Dict[str, Any]:
    """跑完全部自检，返回可 JSON 序列化的结论。"""
    _RESULTS.clear()

    # ---------------- 1. 导入与结构 ----------------
    _section("导入与结构")

    def _imports() -> bool:
        import argus
        from argus import config, dedup, env, errors, lock, msg_parse, paginate, result, selftest

        assert argus.__version__
        assert callable(result.ok) and callable(result.fail)
        assert callable(dedup.advance) and callable(dedup.mark)
        assert callable(paginate.merge_page) and callable(paginate.scroll_history)
        assert callable(env.summary) and callable(env.require_env)
        assert callable(selftest.run)
        assert config is not None and errors is not None and lock is not None and msg_parse is not None
        return True

    _check("核心模块可导入（不需要第三方库）", _imports)

    # ---------------- 2. 错误码 ----------------
    _section("错误码")

    def _codes() -> bool:
        from argus.errors import Err, all_codes, from_state

        codes = all_codes()
        assert len(list(Err)) >= 21, f"错误码数量异常: {len(list(Err))}"
        assert len(set(codes)) == len(codes), "错误码存在重复值"
        assert from_state("wechat_not_running") == "WECHAT_NOT_RUNNING"
        assert from_state("某个没登记的状态") == "TOOL_ERROR"
        assert "DEPENDENCY_MISSING" in codes and "PLATFORM_UNSUPPORTED" in codes
        return True

    _check("错误码枚举完整，未知状态归入 TOOL_ERROR", _codes)

    def _env_exc() -> bool:
        from argus.errors import EnvUnavailable, Err

        exc = EnvUnavailable("缺 pyobjc", Err.DEPENDENCY_MISSING)
        assert isinstance(exc, RuntimeError)
        assert exc.error_code == "DEPENDENCY_MISSING"
        assert str(exc) == "缺 pyobjc"
        assert EnvUnavailable("平台不符", Err.PLATFORM_UNSUPPORTED).error_code == "PLATFORM_UNSUPPORTED"
        return True

    _check("EnvUnavailable 携带错误码", _env_exc)

    # ---------------- 3. 配置 ----------------
    _section("配置")

    def _config() -> bool:
        from argus import config

        assert float(config.cfg("layout.chat_left_ratio")) > 0
        assert int(config.cfg_int("limits.max_text_len", -1)) > 0
        desc = config.describe()
        assert isinstance(desc, dict) and desc
        return True

    _check("外置配置可读取，缺项有默认值", _config)

    # ---------------- 4. 增量去重 ----------------
    _section("增量去重")

    def _dedup_same_text() -> bool:
        from argus.dedup import advance, mark

        page = [_msg("好的"), _msg("好的")]
        marked = mark("自检群", page)
        ids = [m.identity for m in marked]
        assert len(set(ids)) == 2, f"两条相同内容应有两个不同 ID: {ids}"

        store = _store()
        first = advance("自检群", marked, store)
        assert len(first.fresh) == 2, f"首轮应上报 2 条，实际 {len(first.fresh)}"
        assert first.baseline, "首次观测应标记为基线"

        again = advance("自检群", mark("自检群", page), store)
        assert len(again.fresh) == 0, f"重复轮询不应有新消息，实际 {len(again.fresh)}"
        return True

    _check("同人连发相同内容不被吞；重复轮询零误报", _dedup_same_text)

    def _dedup_increment() -> bool:
        from argus.dedup import advance, mark

        store = _store()
        advance("自检群", mark("自检群", [_msg("一"), _msg("二")]), store)
        delta = advance("自检群", mark("自检群", [_msg("一"), _msg("二"), _msg("三")]), store)
        assert len(delta.fresh) == 1, f"应只上报 1 条新增，实际 {len(delta.fresh)}"
        assert delta.fresh[0].payload["text"] == "三"
        assert not delta.watermark_lost, "水位线不应丢失"
        assert delta.matched_level == "L1", f"应精确命中水位线，实际 {delta.matched_level}"
        return True

    _check("新增一条只上报一条", _dedup_increment)

    def _dedup_ts_drift() -> bool:
        """时间戳被 OCR 漏检时，应降级命中而不是把整窗当新消息。"""
        from argus.dedup import advance, mark

        store = _store()
        advance("自检群", mark("自检群", [_msg("甲", ts="10:00"), _msg("乙", ts="10:01")]), store)
        delta = advance(
            "自检群",
            mark("自检群", [_msg("甲", ts=None), _msg("乙", ts=None), _msg("丙", ts=None)]),
            store,
        )
        assert len(delta.fresh) == 1, f"时间戳全丢时仍应只上报 1 条，实际 {len(delta.fresh)}"
        assert delta.reused_weak, "应发生降级匹配"
        assert delta.matched_level in ("L2", "L3"), f"匹配级别异常: {delta.matched_level}"
        return True

    _check("时间戳漏检时降级匹配不重放历史", _dedup_ts_drift)

    # ---------------- 5. 跨屏拼接 ----------------
    _section("跨屏拼接")

    def _merge_overlap() -> bool:
        from argus.paginate import merge_page

        acc = [_msg("B"), _msg("C"), _msg("D")]          # 已累积（较新）
        page = [_msg("A"), _msg("B")]                    # 更早一屏
        merged, k = merge_page(page, acc)
        texts = [m["text"] for m in merged]
        assert k == 1, f"重叠条数应为 1，实际 {k}"
        assert texts == ["A", "B", "C", "D"], f"拼接结果错误: {texts}"
        return True

    _check("重叠锚点拼接不重复", _merge_overlap)

    def _merge_no_overlap() -> bool:
        from argus.paginate import merge_page

        merged, k = merge_page([_msg("X"), _msg("Y")], [_msg("A"), _msg("B")])
        assert k == 0, "无共同锚点时应返回 0，由调用方登记缺口"
        assert [m["text"] for m in merged] == ["X", "Y", "A", "B"]
        return True

    _check("无锚点时按缺口处理而非强行拼接", _merge_no_overlap)

    def _skeleton() -> bool:
        from argus.paginate import _skeleton

        a = _skeleton("S.S 机身 是金属的")
        b = _skeleton("S.S机身是金属的")
        assert a == b, f"骨架归一化未消除空白差异: {a!r} vs {b!r}"
        assert _skeleton("…") == ""
        assert _skeleton("9 这表情绝了") == _skeleton("这表情绝了"), "句首孤立数字未被剥离"
        return True

    _check("骨架归一化消除 OCR 噪声差异", _skeleton)

    # ---------------- 6. 消息解析纯逻辑 ----------------
    _section("消息解析")

    def _timestamps() -> bool:
        from argus.msg_parse import _is_timestamp, _is_quote_label

        assert _is_timestamp("12:30")
        assert _is_timestamp("昨天 12:30")
        assert _is_timestamp("星期一 09:05")
        assert not _is_timestamp("这个是12:30开始的吗")
        assert _is_quote_label("某某说：")
        return True

    _check("时间戳识别与引用标签判定", _timestamps)

    # ---------------- 7. 并发闸门 ----------------
    _section("并发闸门")

    def _lock_busy() -> bool:
        import threading

        from argus import lock
        from argus.result import ToolResult

        @lock.serialized(timeout=0.05)
        def _dummy() -> ToolResult:
            return ToolResult(success=True)

        # 必须换一个线程来验证：锁是可重入的，同一线程重复取锁会成功，
        # 那样测的就不是「重叠调用」了。
        box: Dict[str, Any] = {}

        def _worker() -> None:
            box["res"] = _dummy()

        assert lock.try_acquire(0.1)
        try:
            t = threading.Thread(target=_worker)
            t.start()
            t.join(5.0)
        finally:
            lock.release()
        res = box.get("res")
        assert res is not None, "工作线程未返回结果"
        assert res.success is False, "持锁期间的重叠调用应失败"
        assert res.error_code == "BUSY", f"应返回 BUSY，实际 {res.error_code}"
        assert _dummy().success is True, "释放后应可正常执行"
        return True

    _check("重叠调用返回 BUSY 而非互相打断", _lock_busy)

    # ---------------- 8. 环境诊断 ----------------
    _section("环境诊断")

    def _env_report() -> bool:
        from argus import env

        rep = env.summary()
        for key in ("ready", "platform", "python", "dependencies", "missing", "problems"):
            assert key in rep, f"诊断结果缺少字段 {key}"
        assert isinstance(rep["dependencies"], list) and rep["dependencies"]
        assert all("module" in d and "ok" in d for d in rep["dependencies"])
        if rep["missing"] or not rep["platform_ok"] or not rep["python_ok"]:
            assert rep["ready"] is False, "依赖不全时 ready 必须为 False"
        return True

    _check("环境诊断结论自洽且可序列化", _env_report)

    def _graceful() -> bool:
        """环境不满足时，工具层应返回结构化错误码而不是抛异常。"""
        from argus import lock
        from argus.errors import EnvUnavailable, Err
        from argus.result import ToolResult

        @lock.serialized(timeout=2.0)
        def _needs_env() -> ToolResult:
            raise EnvUnavailable("模拟缺少 pyobjc", Err.DEPENDENCY_MISSING)

        res = _needs_env()
        assert res.success is False
        assert res.error_code == "DEPENDENCY_MISSING"
        assert "pyobjc" in (res.error or "")
        return True

    _check("缺依赖时返回错误码而非 traceback", _graceful)

    passed = sum(1 for r in _RESULTS if r["ok"] and not r.get("section"))
    failed = sum(1 for r in _RESULTS if not r["ok"])
    return {
        "success": failed == 0,
        "passed": passed,
        "failed": failed,
        "total": passed + failed,
        "checks": _RESULTS,
    }


def main() -> int:
    import json

    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["success"] else 1
