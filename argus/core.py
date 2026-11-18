"""核心封装层：窗口捕获、OCR、布局解析、输入动作、状态检测。

设计要点：
1. 复用 wechat-mac-rpa 的视觉能力，不重写识别算法。
2. 修正原项目两个适配缺陷：窗口最小宽度误判、cliclick 硬依赖。
3. 增加 UI 状态检测（搜一搜面板污染、后台/遮挡），避免在异常布局下返回错误数据。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from typing import Any, Optional

# 复用原项目模块。
# 注意：Apple 框架与感知层依赖一律采用「允许缺失」的导入方式。
# 这样在没有 pyobjc / Pillow / numpy 的环境里，``import argus`` 仍然成功，
# 纯逻辑层（去重、拼接、错误码、配置、CLI 诊断、单元测试）照常可用；
# 只有真正要读写屏幕时，才会由 require_env() 明确抛出 DEPENDENCY_MISSING。
from argus.vendor.profile import LayoutProfile, PROFILE_WECHAT_MAC_1760X1280
from argus.vendor.models import PerceptionResult

from .config import cfg, cfg_float, cfg_int, describe as describe_config
from .env import require_env
from .errors import EnvUnavailable, Err
from .log import get_logger

try:
    import AppKit
    import Quartz
except Exception as _exc:  # noqa: BLE001 pyobjc 版本不匹配时也可能抛非 ImportError
    AppKit = None  # type: ignore[assignment]
    Quartz = None  # type: ignore[assignment]
    APPLE_FRAMEWORKS_ERROR: Optional[str] = f"{type(_exc).__name__}: {_exc}"
else:
    APPLE_FRAMEWORKS_ERROR = None

try:
    from argus.vendor.window_capture import (  # noqa: F401
        WeChatNotReadyError,
        WindowCapture,
        WindowNotFoundError,
    )
    from argus.vendor.layout_parser import LayoutParser  # noqa: F401
    from argus.vendor.extractor import MessageExtractor  # noqa: F401
    from argus.vendor.vision_ocr import VisionOCREngine  # noqa: F401
except Exception as _exc:  # noqa: BLE001
    WeChatNotReadyError = WindowNotFoundError = None  # type: ignore[assignment]
    WindowCapture = LayoutParser = MessageExtractor = VisionOCREngine = None  # type: ignore[assignment]
    PERCEPTION_ERROR: Optional[str] = f"{type(_exc).__name__}: {_exc}"
else:
    PERCEPTION_ERROR = None

logger = get_logger("argus.core")

BUNDLE_ID = "com.tencent.xinWeChat"
CLICLICK_CANDIDATES = (
    "/opt/homebrew/bin/cliclick",
    "/usr/local/bin/cliclick",
)

#: CGEventFlags 中「Command 键」掩码的数值（CoreGraphics CGEventTypes.h：
#: kCGEventFlagMaskCommand = 0x00100000）。写成字面量而不是引用 Quartz 常量，
#: 是为了让类体在缺少 pyobjc 的环境里也能完成定义。
CMD_FLAG = 0x00100000

# 判定窗口可用的最小尺寸。原项目固定 800x600，会把 772x640 的正常窗口误判为未就绪。
MIN_WINDOW_WIDTH = 600
MIN_WINDOW_HEIGHT = 480

# 群聊标题形如「XXX（178）」，括号内为成员数
_GROUP_TITLE_RE = re.compile(r"[（(]\d{1,5}[）)]")

# 微信 4.x 自身消息气泡实测为 (146,240,148)，而原项目 profile 配的是 3.8 时期的
# (176,240,167)，红通道差 30、蓝通道差 19，导致 self_bubbles 恒为空、自己发的消息
# 被全部误判为「对方」。这里以原 profile 为基，仅替换颜色相关字段。
PROFILE_WECHAT_MAC_4X = replace(
    PROFILE_WECHAT_MAC_1760X1280,
    name="wechat_mac_4x_dynamic",
    self_green=(146, 240, 148),
    self_green_tolerance=45,
)


def build_profile(img_w: int, img_h: int) -> LayoutProfile:
    """按截图实际像素尺寸构造布局 profile。

    原项目的 LayoutParser 虽然会对绝对坐标做缩放，但其内部仍有若干阈值直接引用
    profile.window_width（如消息聚类 x_threshold），窗口尺寸变化后这些阈值不会跟随。
    把基准直接设为实际尺寸（缩放系数恒为 1），可让所有阈值一致地基于真实像素。
    """
    base = PROFILE_WECHAT_MAC_4X
    sx = img_w / base.window_width
    # 边界从配置读：换显示器缩放或改窗口大小后，只调 profile_default.json 即可
    return replace(
        base,
        name=f"argus_dynamic_{img_w}x{img_h}",
        window_width=img_w,
        window_height=img_h,
        left_boundary=int(img_w * cfg_float("layout.chat_left_ratio", base.left_boundary / base.window_width)),
        chat_list_x_max=int(img_w * cfg_float("layout.chat_list_right_ratio", base.chat_list_x_max / base.window_width)),
        # 标题栏高度是固定的界面像素，不随窗口缩放
        title_y_max=cfg_int("layout.title_bar_px", base.title_y_max),
        input_y_min=int(img_h - cfg_int("layout.input_bottom_margin_px", base.window_height - base.input_y_min)),
    )


@dataclass
class UIState:
    status: str                 # ok | search_panel_open | window_not_found | wechat_not_running | not_ready
    window_rect: Any = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.status == "ok"


class Clicker:
    """鼠标点击。cliclick 存在则用 cliclick，否则退回 CGEvent，并明确记录降级原因。"""

    def __init__(self) -> None:
        self.cliclick = next((p for p in CLICLICK_CANDIDATES if os.path.exists(p)), None)
        self.degraded = self.cliclick is None
        self._last_point: tuple[int, int] | None = None
        self._same_point_streak = 0
        if self.degraded:
            logger.warning(
                "cliclick 未安装，已降级为 CGEvent 点击。建议安装：brew install cliclick"
            )

    def click(self, x: int, y: int) -> tuple[bool, str]:
        if self._last_point == (x, y):
            self._same_point_streak += 1
        else:
            self._same_point_streak = 0
        self._last_point = (x, y)
        # 防连点：同一点位连续点击超过 2 次视为异常
        if self._same_point_streak > 2:
            return False, f"同一坐标连续点击次数超限，已阻止：({x},{y})"

        if self.cliclick:
            try:
                subprocess.run(
                    [self.cliclick, f"c:{x},{y}"],
                    check=True,
                    timeout=5,
                    capture_output=True,
                )
                return True, ""
            except (subprocess.SubprocessError, OSError) as exc:
                logger.warning("cliclick 点击失败，降级 CGEvent: %s", exc)

        try:
            point = Quartz.CGPoint(x, y)
            for ev_type in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
                ev = Quartz.CGEventCreateMouseEvent(None, ev_type, point, 0)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.05)
            return True, "cliclick 不可用，已降级 CGEvent" if self.degraded else ""
        except Exception as exc:  # noqa: BLE001
            return False, f"CGEvent 点击异常: {exc}"


class WeChatCore:
    """微信桌面状态读取与交互的底层封装。"""

    def __init__(self) -> None:
        # 先校验环境：缺 pyobjc / Pillow / numpy 时在这里就抛出可读的错误，
        # 而不是等到第一次截图时炸出一段 traceback。
        require_env()
        if APPLE_FRAMEWORKS_ERROR or PERCEPTION_ERROR:
            raise EnvUnavailable(
                "感知层不可用：" + (APPLE_FRAMEWORKS_ERROR or PERCEPTION_ERROR or "未知原因"),
                Err.DEPENDENCY_MISSING,
            )
        self.profile = PROFILE_WECHAT_MAC_4X
        self.capture = WindowCapture(
            min_effective_width=MIN_WINDOW_WIDTH,
            min_effective_height=MIN_WINDOW_HEIGHT,
        )
        self.ocr = VisionOCREngine()
        self.layout = LayoutParser(self.profile)
        self.extractor = MessageExtractor(self.profile)
        self.clicker = Clicker()
        self._last: Optional[PerceptionResult] = None
        self._last_image: Optional[str] = None
        self._img_size: Optional[tuple[int, int]] = None

    def _ensure_profile(self, img_w: int, img_h: int) -> None:
        """图片尺寸变化时重建布局解析器，保证阈值与实际像素一致。"""
        if self._img_size == (img_w, img_h):
            return
        self.profile = build_profile(img_w, img_h)
        self.layout = LayoutParser(self.profile)
        self.extractor = MessageExtractor(self.profile)
        self._img_size = (img_w, img_h)
        logger.info(
            "布局 profile 已按实际尺寸重建: %sx%s (left_boundary=%s, input_y_min=%s)",
            img_w,
            img_h,
            self.profile.left_boundary,
            self.profile.input_y_min,
        )

    # ---------- 基础状态 ----------

    @staticmethod
    def is_wechat_running() -> bool:
        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_ID)
        return len(apps) > 0

    @staticmethod
    def activate() -> bool:
        """把微信置于前台。

        macOS 14 起 activateWithOptions_ 的语义收紧，后台进程调用常返回 True
        但实际不生效。因此按「API → open -b → AppleScript」三级降级。
        """
        try:
            apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_ID)
            if apps and apps[0].activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps):
                time.sleep(0.2)
                if WeChatCore.frontmost_is_wechat():
                    return True
        except Exception:  # noqa: BLE001
            pass

        try:
            subprocess.run(["open", "-b", BUNDLE_ID], capture_output=True, timeout=6)
            time.sleep(0.4)
            if WeChatCore.frontmost_is_wechat():
                return True
        except subprocess.SubprocessError:
            pass

        try:
            script = f'tell application id "{BUNDLE_ID}" to activate'
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=6)
            time.sleep(0.4)
        except subprocess.SubprocessError:
            pass
        return WeChatCore.frontmost_is_wechat()

    @staticmethod
    def frontmost_is_wechat() -> bool:
        try:
            from AppKit import NSWorkspace

            active = NSWorkspace.sharedWorkspace().frontmostApplication()
            return bool(active and active.bundleIdentifier() == BUNDLE_ID)
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def user_idle_seconds() -> float:
        """距最近一次用户输入（键盘/鼠标）的秒数。"""
        try:
            return float(
                Quartz.CGEventSourceSecondsSinceLastEventType(
                    Quartz.kCGEventSourceStateHIDSystemState,
                    Quartz.kCGAnyInputEventType,
                )
            )
        except Exception:  # noqa: BLE001
            return 999.0

    def ensure_frontmost(self, max_wait: float = 20.0, idle_gate: float = 1.5) -> bool:
        """确保微信在前台。

        实测结论：用户在操作其他应用时，macOS 26 会拒绝后台进程夺取焦点，
        NSRunningApplication.activateWithOptions_ / open -b / osascript 三者
        均返回成功但实际无效。直接对抗只会失败，且会打断用户。

        因此这里采用「空闲闸门」：等待用户停止输入 idle_gate 秒后再尝试激活，
        在 max_wait 秒预算内反复尝试。用户一直在操作时宁可让调用方超时，
        也不强行抢占焦点。
        """
        deadline = time.time() + max_wait
        last_try = 0.0
        while time.time() < deadline:
            if self.frontmost_is_wechat():
                return True
            if self.user_idle_seconds() >= idle_gate and time.time() - last_try > 0.6:
                last_try = time.time()
                self.activate()
                time.sleep(0.35)
            else:
                time.sleep(0.25)
        return self.frontmost_is_wechat()

    # ---------- 感知 ----------

    def perceive(self) -> tuple[Optional[PerceptionResult], UIState]:
        """执行一次完整感知，返回结果与 UI 状态。"""
        if not self.is_wechat_running():
            return None, UIState("wechat_not_running", detail="微信未运行")

        try:
            cap = self.capture.capture()
        except WeChatNotReadyError as exc:
            return None, UIState("not_ready", detail=str(exc))
        except WindowNotFoundError as exc:
            return None, UIState("window_not_found", detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            return None, UIState("window_not_found", detail=f"{type(exc).__name__}: {exc}")

        self._last_image = cap.image_path
        try:
            from PIL import Image

            with Image.open(cap.image_path) as im:
                img_w, img_h = im.size
            self._ensure_profile(img_w, img_h)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取截图尺寸失败，沿用现有布局 profile: %s", exc)

        elements = self.ocr.recognize(cap.image_path)
        layout = self.layout.parse(elements, cap.image_path)

        # 面板污染检测：右侧出现搜一搜特征，说明聊天区被挤压，解析结果不可信
        panel = self._detect_search_panel(elements, cap.image_path)
        if panel:
            return None, UIState(
                "search_panel_open",
                window_rect=cap.window_rect,
                detail=panel,
            )

        # 优先用基于像素证据的解析器还原发送者与时间戳；
        # 原项目 extractor 依赖固定 x 坐标带判断昵称，在窄窗口/4.x 布局下失效，
        # 会退化为「全部记为对方、时间戳为空」。解析失败时回退原实现。
        messages = self._parse_messages(elements, cap.image_path, layout)
        # 群聊判定：标题带成员数，或消息中出现了具体昵称（说明有人名可读）
        chat_title = layout.chat_name or ""
        is_group = bool(_GROUP_TITLE_RE.search(chat_title)) or any(
            m.sender not in ("自己", "对方", "系统") for m in messages
        )
        result = PerceptionResult(
            chat_name=layout.chat_name,
            messages=messages,
            chat_list_items=layout.chat_list_items,
            window_rect=cap.window_rect,
            screenshot_path=cap.image_path,
            scale_factor=cap.scale_factor,
            is_group=is_group,
            is_service_account_list=layout.is_service_account_list,
            debug_info=self.layout.debug_info,
        )
        self._last = result
        return result, UIState("ok", window_rect=cap.window_rect)

    def _parse_messages(self, elements, image_path: str, layout):
        try:
            import numpy as np
            from PIL import Image

            from .msg_parse import parse_messages, recover_timestamps

            with Image.open(image_path) as im:
                arr = np.array(im.convert("RGB"))

            chat_left = float(
                getattr(self.profile, "left_boundary", arr.shape[1] * cfg_float("layout.chat_left_ratio", 0.2727))
            )
            input_y = float(
                getattr(self.profile, "input_y_min", arr.shape[0] - cfg_int("layout.input_bottom_margin_px", 240))
            )
            # 标题栏高度相对固定，不随窗口缩放；截图是 2 倍图，故乘 2
            chat_top = float(getattr(self.profile, "title_y_max", cfg_int("layout.title_bar_px", 70))) * 2.0
            messages = parse_messages(
                elements=elements,
                arr=arr,
                chat_left=chat_left,
                chat_bottom=input_y - 16,
                chat_name=layout.chat_name,
                self_bubbles=getattr(layout, "self_bubbles", None),
                chat_top=chat_top,
            )
            if messages:
                # 主识别没拿到任何时间戳时，做一次对比度增强的补充识别。
                # 微信时间戳是浅灰小字，Vision OCR 在原始截图上常漏检。
                if all(m.timestamp is None for m in messages):
                    recovered = recover_timestamps(
                        image_path, chat_left, chat_top, input_y - 16
                    )
                    if recovered:
                        for m in messages:
                            if not m.source_elements:
                                continue
                            my = m.source_elements[0].center.y
                            best = None
                            for ty, txt in recovered:
                                if ty < my and (best is None or ty > best[0]):
                                    best = (ty, txt)
                            if best:
                                m.timestamp = best[1]
                return messages
            logger.info("像素解析未产出消息，回退原 extractor")
        except Exception as exc:  # noqa: BLE001
            logger.warning("像素解析失败，回退原 extractor: %s", exc)
        return self.extractor.extract(layout)

    def _detect_search_panel(self, elements, image_path: str) -> str:
        """检测「搜一搜」侧面板是否占据聊天区。"""
        width = 0
        try:
            from PIL import Image

            width = Image.open(image_path).size[0]
        except Exception:  # noqa: BLE001
            width = 0

        markers = ("搜一搜", "AI搜索", "搜索发现")
        for e in elements:
            text = (e.text or "").strip()
            if not any(m in text for m in markers):
                continue
            # 仅在右半区命中时判定为面板污染
            if width and e.center.x > width * cfg_float("layout.search_panel_x_ratio", 0.55):
                return f"检测到右侧搜一搜面板（文本 {text!r}），聊天区被压缩，请先关闭该面板"
        return ""

    # ---------- 坐标换算 ----------

    def to_screen(self, px: float, py: float, rect=None) -> tuple[int, int]:
        """截图坐标（Retina 物理像素）转屏幕逻辑坐标。"""
        if rect is None:
            if self._last is None:
                raise RuntimeError("缺少窗口信息，无法换算坐标")
            rect = self._last.window_rect
        scale = 2.0
        try:
            screen = AppKit.NSScreen.mainScreen()
            if screen is not None:
                scale = float(screen.backingScaleFactor()) or 2.0
        except Exception:  # noqa: BLE001
            pass
        return int(rect.x + px / scale), int(rect.y + py / scale)

    # ---------- 输入动作 ----------

    def click_screenshot_point(self, px: float, py: float) -> tuple[bool, str]:
        x, y = self.to_screen(px, py)
        if not self.ensure_frontmost():
            return False, "微信无法置于前台，已拒绝点击"
        return self.clicker.click(x, y)

    def press_key(self, key_spec: str) -> tuple[bool, str]:
        # 前台守卫：微信不在前台时绝不投递按键，否则内容会落进别的应用
        if not self.ensure_frontmost():
            return False, "微信不在前台，已拒绝投递按键"

        script = f'''
            tell application "System Events"
                tell process "WeChat"
                    {key_spec}
                end tell
            end tell
        '''
        try:
            proc = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=6
            )
            if proc.returncode == 0:
                return True, ""
            apple_err = (proc.stderr or "").strip()[:200]
        except subprocess.SubprocessError as exc:
            apple_err = f"osascript 执行失败: {exc}"

        # AppleScript 路径失败（常见为 System Events 权限违例 -10004）时，
        # 改用 CGEvent 直接投递按键事件。CGEvent 只需辅助功能权限，
        # 不依赖「自动化」权限，与点击的降级策略保持一致。
        fallback_ok, fallback_err = self._press_key_cgevent(key_spec)
        if fallback_ok:
            return True, f"AppleScript 失败已降级 CGEvent: {apple_err}"
        return False, f"{apple_err} | CGEvent 亦失败: {fallback_err}"

    # 常用按键 -> (虚拟键码, 修饰键标志)
    # 这里用数值常量而不是 Quartz.kCGEventFlagMaskCommand：类体在模块导入时就会求值，
    # 一旦环境里没有 pyobjc，引用 Quartz 会让整个模块导入失败。该数值取自
    # CoreGraphics 的 CGEventTypes.h（kCGEventFlagMaskCommand = 0x00100000）。
    _KEYMAP = {
        'keystroke "v" using command down': (9, CMD_FLAG),
        'keystroke "a" using command down': (0, CMD_FLAG),
        "key code 36": (36, 0),
        "key code 53": (53, 0),
        "key code 51": (51, 0),  # Delete（向前删除），清空输入框用
    }

    def _press_key_cgevent(self, key_spec: str) -> tuple[bool, str]:
        spec = " ".join(key_spec.split())
        if spec not in self._KEYMAP:
            return False, f"CGEvent 无对应键位映射: {spec}"
        code, flags = self._KEYMAP[spec]
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        try:
            for ev_type in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
                ev = Quartz.CGEventCreateKeyboardEvent(src, code, ev_type == Quartz.kCGEventKeyDown)
                if flags:
                    Quartz.CGEventSetFlags(ev, flags)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.04)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:160]

    def paste_text(self, text: str) -> tuple[bool, str]:
        """通过剪贴板粘贴文本（保留原剪贴板内容）。"""
        original = ""
        try:
            proc = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
            if proc.returncode == 0:
                original = proc.stdout
        except subprocess.SubprocessError:
            pass

        try:
            p = subprocess.run(["pbcopy"], input=text, text=True, timeout=5)
            if p.returncode != 0:
                return False, "pbcopy 写入失败"
        except subprocess.SubprocessError as exc:
            return False, f"pbcopy 异常: {exc}"

        ok_key, err = self.press_key('keystroke "v" using command down')
        time.sleep(0.45)
        try:
            subprocess.run(["pbcopy"], input=original, text=True, timeout=5)
        except subprocess.SubprocessError:
            pass
        return ok_key, err

    def scroll(self, px: float, py: float, lines: int, move_cursor: bool = True) -> None:
        """在截图物理像素坐标 (px, py) 处滚动。

        实测结论：滚轮事件按**真实光标位置**路由，``CGEventSetLocation``
        只设置事件自身坐标，不移动光标。光标不在聊天区时，滚动会被光标下方的
        控件（例如左侧会话列表）吃掉，表现为「滚了但内容不变」。
        因此默认先把光标移到目标点再投递滚轮。
        px/py 必须是截图物理像素坐标（2x），不是窗口逻辑点。
        """
        x, y = self.to_screen(px, py)
        if move_cursor:
            mv = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved, Quartz.CGPoint(x, y),
                Quartz.kCGMouseButtonLeft,
            )
            if mv is not None:
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, mv)
                time.sleep(0.06)
        ev = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitLine, 1, lines
        )
        Quartz.CGEventSetLocation(ev, Quartz.CGPoint(x, y))
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.35)

    def scroll_ratio(
        self, rx: float, ry: float, lines: int, move_cursor: bool = True
    ) -> None:
        """按窗口内相对位置滚动，rx/ry 取 0~1。

        调用方不需要关心 Retina 缩放 —— 这里统一换算成截图物理像素，
        避免把逻辑点当成物理像素导致焦点算错位置（曾因此把焦点落到会话列表上）。
        """
        if self._last is None:
            raise RuntimeError("缺少窗口信息，无法换算滚动焦点")
        rect = self._last.window_rect
        self.scroll(rect.width * 2 * rx, rect.height * 2 * ry, lines, move_cursor)

    @staticmethod
    def cursor_pos() -> tuple[float, float]:
        try:
            p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            return float(p.x), float(p.y)
        except Exception:  # noqa: BLE001
            return (0.0, 0.0)

    @staticmethod
    def move_cursor(x: float, y: float) -> None:
        ev = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, Quartz.CGPoint(x, y),
            Quartz.kCGMouseButtonLeft,
        )
        if ev is not None:
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    # ---------- 输入框内容校验与可信发送 ----------

    def read_input_box(self) -> tuple[str, str]:
        """OCR 读取输入框内的文字，返回 (合并文本, 截图路径)。

        只采集输入框正文区域，避开底部工具条图标与「发送」按钮，
        以免图标被识别成乱码字符干扰比对。
        """
        cap = self.capture.capture()
        w = cap.window_rect.width * 2
        h = cap.window_rect.height * 2
        els = self.ocr.recognize(cap.image_path)
        placeholders = ("长按", "语音输入", "发送")
        # 边界按实测校准：左侧会话列表文字一直延伸到 x≈0.40w，
        # 输入框正文从 x≈0.42w 起；y 下限 0.80h 可避开上方最后一条消息气泡。
        bx = cfg("layout.input_box", {}) or {}
        x_min = float(bx.get("x_min", 0.40)) * w
        x_max = float(bx.get("x_max", 0.86)) * w
        y_min = float(bx.get("y_min", 0.80)) * h
        y_max = float(bx.get("y_max", 0.905)) * h
        parts = [
            e.text.strip()
            for e in els
            if x_min < e.center.x < x_max
            and y_min < e.center.y < y_max
            and not any(p in e.text for p in placeholders)
        ]
        return " ".join(p for p in parts if p), cap.image_path

    @staticmethod
    def _norm_for_compare(text: str) -> str:
        drop = set(" \t\n　，,。.、；;：:？！?!“”\"'‘’（）()【】[]《》<>…—-~·")
        return "".join(ch for ch in text if ch not in drop)

    @staticmethod
    def _core_probe(text: str, n: int = 8) -> str:
        """取正文中的汉字/字母/数字片段作为比对探针。

        OCR 对 emoji、符号的识别不稳定（例如 😄 会被读成「9」），
        因此比对只用正文片段，避免符号噪声造成误判。
        """
        out = []
        for ch in text:
            if "\u4e00" <= ch <= "\u9fff" or ch.isalnum():
                out.append(ch)
            if len(out) >= n:
                break
        return "".join(out)

    def _tap_key(self, code: int, times: int = 1, flag: int = 0) -> None:
        """投递单键（可带修饰键）。修饰键以真实的 keyDown/keyUp 成对发送，
        比仅设置 flags 更接近物理按键，兼容性更好。"""
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        mods = []
        if flag & Quartz.kCGEventFlagMaskCommand:
            mods.append(55)  # Command
        for _ in range(times):
            for m in mods:
                Quartz.CGEventPost(
                    Quartz.kCGHIDEventTap,
                    Quartz.CGEventCreateKeyboardEvent(src, m, True),
                )
                time.sleep(0.02)
            for ev_type in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
                ev = Quartz.CGEventCreateKeyboardEvent(src, code, ev_type == Quartz.kCGEventKeyDown)
                if flag:
                    Quartz.CGEventSetFlags(ev, flag)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.015)
            for m in reversed(mods):
                Quartz.CGEventPost(
                    Quartz.kCGHIDEventTap,
                    Quartz.CGEventCreateKeyboardEvent(src, m, False),
                )
                time.sleep(0.02)

    def clear_input_box(self) -> tuple[bool, str]:
        """清空输入框：聚焦 -> 全选删除 -> 复核。

        CGEvent 的 Cmd+A 在部分应用里不生效，因此先试全选删除，
        复核不通过则退化为逐字符退格，确保不会在残留草稿上继续粘贴。
        """
        result, state = self.perceive()
        if not state.usable or state.window_rect is None:
            return False, state.detail or state.status
        rect = state.window_rect
        ok, err = self.click_screenshot_point((rect.width * 2) / 2.0, (rect.height * 2) - 60 * 2)
        if not ok:
            return False, f"聚焦输入框失败: {err}"
        time.sleep(0.35)

        # 方案一：全选后删除
        self._tap_key(0, 1, Quartz.kCGEventFlagMaskCommand)  # Cmd+A
        time.sleep(0.25)
        self._tap_key(51, 1)  # Delete
        time.sleep(0.35)
        left, _ = self.read_input_box()
        if not self._norm_for_compare(left):
            return True, ""

        # 方案二：逐字符退格（覆盖 multi-line 草稿）
        self._tap_key(0, 1, Quartz.kCGEventFlagMaskCommand)
        time.sleep(0.2)
        self._tap_key(51, 260)
        time.sleep(0.5)
        left2, _ = self.read_input_box()
        if not self._norm_for_compare(left2):
            return True, "全选失效，已用逐字符退格清空"
        return False, f"输入框未能清空，残留: {left2[:40]}"

    def send_text_verified(self, text: str) -> tuple[bool, str]:
        """可信发送：粘贴 -> 校验输入框内容 -> 点「发送」-> 复核消息上屏。

        任何一步与预期不符即中止，不点击发送。
        """
        if not self.ensure_frontmost():
            return False, "微信不在前台，已中止"

        cleared, cerr = self.clear_input_box()
        if not cleared:
            return False, f"发送前清空失败: {cerr}"

        pasted, perr = self.paste_text(text)
        if not pasted:
            return False, f"粘贴失败: {perr}"
        time.sleep(0.5)

        got, shot = self.read_input_box()
        expect_n = self._norm_for_compare(text)
        got_n = self._norm_for_compare(got)
        probe = self._core_probe(text)
        if not got_n:
            return False, f"输入框校验失败：未识别到任何文字（{shot}）"
        if probe and probe not in got_n:
            return False, f"输入框校验失败：正文片段「{probe}」未出现在输入框中。实际「{got_n[:24]}」"
        if len(got_n) > len(expect_n) * 1.5 + 12:
            return False, (
                f"输入框校验失败：内容明显多于预期（预期 {len(expect_n)} 字，"
                f"实际 {len(got_n)} 字）。实际内容「{got[:60]}」"
            )

        # 定位并点击「发送」按钮，避免依赖回车键（不同微信版本的发送键不一致）
        els = self.ocr.recognize(shot)
        cap_rect = self._last.window_rect if self._last is not None else None
        if cap_rect is None:
            return False, "缺少窗口信息，无法定位发送按钮"
        w = cap_rect.width * 2
        h = cap_rect.height * 2
        btn = next(
            (
                e
                for e in els
                if "发送" in e.text
                and e.center.x > 0.80 * w
                and e.center.y > 0.88 * h
            ),
            None,
        )
        if btn is None:
            return False, "未定位到「发送」按钮，已中止（未发送）"

        ok, err = self.click_screenshot_point(btn.center.x, btn.center.y)
        if not ok:
            return False, f"点击发送按钮失败: {err}"
        time.sleep(1.3)

        after, shot2 = self.read_input_box()
        if self._norm_for_compare(after):
            return False, f"点击发送后输入框仍有内容，可能未发出: {after[:40]}"
        return True, f"已发送并通过输入框清空校验（{shot2}）"


_core: Optional[WeChatCore] = None


def core() -> WeChatCore:
    global _core
    if _core is None:
        _core = WeChatCore()
    return _core
