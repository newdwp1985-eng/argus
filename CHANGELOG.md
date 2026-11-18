# Changelog · 变更记录

本文件记录 Argus 的全部公开变更。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

「已知边界」一节记录的是**当前版本明确不解决的问题**，不是待修 bug —— 它们是路线约束，
写在这里是为了让使用者在选型阶段就能判断适不适用，而不是上线后才发现。

---

## [Unreleased]

以下为已确认的改进方向，**在 1.0.0 中均未实现**，列在此处仅为记录，不作为承诺：

- 非文本消息占位：图片 / 表情 / 语音 / 文件在消息列表中显式标记为 `[非文本消息]`，
  让条数差异可解释，而不是静默丢条。
- 昵称截断标记：当昵称在会话列表中带省略号时，返回 `sender_truncated: true`。
- 活跃群回溯上限：当前判定到顶的方式是「连续两次滚动无位移」，活跃大群在 12 屏内
  仍会遇到持续新增内容导致的长尾；计划改为按时间戳下界判定停止条件。
- 真实消息标识：若上游微信版本提供可读的消息序列号，替换现有的哈希 + 序号构造。

---

## [1.0.1] — 2026-11-18

修复技能市场沙箱评测不通过的问题。根因不是功能缺陷，而是**环境耦合**：1.0.0 在
`import` 阶段就强制加载 pyobjc / Pillow / numpy，任何干净机器上第一行 import 就抛
`ModuleNotFoundError: No module named 'AppKit'`。评测方看到的是崩溃，而不是
「这项能力在当前环境不适用」。

### 修复：把「能不能跑」从「跑得对不对」里剥出来

- Apple 框架（AppKit / Quartz）与感知层模块（window_capture / layout_parser /
  extractor / vision_ocr）改为**允许缺失**的导入：导入失败只记录原因，不阻断模块加载。
- `msg_parse` 的 numpy 改为按需导入，缺失时抛 `EnvUnavailable` 而非 `ImportError`。
- `argus.TOOLS` 改为惰性导出，`import argus` 不再连带加载整个工具层。
- `WeChatCore._KEYMAP` 原先在类体里引用 `Quartz.kCGEventFlagMaskCommand`，类体在导入期
  求值，等于把 pyobjc 变成硬依赖。改为数值常量 `CMD_FLAG = 0x00100000`
  （CoreGraphics `CGEventTypes.h` 中该掩码的取值）。
- 工具层的串行装饰器捕获 `EnvUnavailable`，转成 `DEPENDENCY_MISSING` /
  `PLATFORM_UNSUPPORTED` 错误码返回。任何环境下调用 9 个工具，拿到的都是可解析 JSON。
- MCP 入口缺 `mcp` 包时，向 stderr 输出结构化错误并 `exit 2`，不再抛导入堆栈。

### 新增

- `argus/env.py`：纯标准库的环境判定。列出平台、Python 版本、7 项依赖的安装名与缺失
  影响面（哪个工具不可用）、可选外部工具（cliclick），并给出 `ready` 与 `install_hint`。
- `argus doctor`：打印上述诊断 JSON。默认退出码 0（诊断跑完即成功，就绪与否写在
  `ready` 字段），`--strict` 供 CI 用，`--no-runtime` 跳过运行期探测。
- `argus self-test`：14 项零依赖断言，覆盖错误码、配置、去重（含时间戳漏检降级）、
  拼接（含无锚点缺口）、并发闸门、环境诊断与降级路径。
- `tests/test_env.py`：28 项断言，钉住「导入不连带工具层」「缺依赖返回错误码」
  「doctor/self-test 在任何环境可跑通」三条契约。
- 错误码 19 → 21（新增 `PLATFORM_UNSUPPORTED`、`DEPENDENCY_MISSING`）。
- `SKILL.md` 增「运行前提」章节，把前提分成 L1 纯逻辑 / L2 感知层 / L3 端到端三层，
  并写明每层的验证命令与「沙箱里能验证什么」。

### 修正

- `SKILL.md` 命令表里的 `-n` 改为 CLI 实际支持的 `--limit`（原文照抄示例会直接报错）。

### 验证

在**未安装任何第三方包**的 venv 中实测：四套测试共 94 条断言全部通过；
`argus doctor` 输出 `ready: false` 且退出码 0；`argus self-test` 14/14 通过；
`argus status` 返回 `DEPENDENCY_MISSING` 而不是 traceback。有依赖的真机上
`status`、`list` 行为与 1.0.0 一致。

---

## [1.0.0] — 2026-11-15

首个公开版本。MCP 服务与 CLI 双入口，共 9 个工具。

### 新增

**感知层**
- 窗口截图（`screencapture -l <windowid>`）→ Apple Vision 本地 OCR → 布局解析 →
  结构化消息（昵称与正文按墨色亮度区分）的完整链路。
- 坐标换算显式化：窗口边界是逻辑点，截图是物理像素（Retina 约 2.0 倍），
  两者之间的换算集中在 `core.py`，不散落在调用点。
- 布局参数外置至 `profile_default.json`（`layout` / `input_box` / `scroll` /
  `limits` / `thresholds`），支持按 mtime 热重载。

**增量与历史**
- 水位线去重（`dedup.py`）：弱指纹（含 / 不含时间戳两版）+ 出现序号构造
  `message_id = {hash}:{occurrence}`，每会话持久化水位线。定位采用三级降级
  （identity → weak → weak_no_ts），三级均失败时上报 `watermark_lost`，
  而不是当作全量新消息重放。
- 跨屏回溯（`paginate.py`）：滚动方向自校准、骨架归一化（去空白 / 去标点 /
  转小写 / 剥离句首孤立数字）、相似度 0.82 兜底匹配、最长重叠拼接、缺口检测、
  「连续两次无位移」判到顶、结束归位。

**投递与安全**
- 投递结果不折叠成布尔值：`send_message` 分别返回 `sent`（按钮点击 + 输入框清空校验）
  与 `confirmed_in_ui`（发送后在消息区读回该内容）。后者为假时附 `warnings` 要求人工核对，
  真实失败返回 `SEND_UNVERIFIED`，不返回 `sent: true`。
- 发送走 `send_text_verified`：回读输入框比对内容 → OCR 定位「发送」按钮 →
  发送后复核输入框是否清空，`confirmed_in_ui` 记录界面证据。
- 发送前置校验：必须 `confirm=true`；联系人无法唯一确定时拒绝并返回候选；
  空消息 / 超 2000 字 / 30 秒内重复内容拒绝；微信不在前台返回
  `WECHAT_NOT_FRONTMOST` 并拒绝粘贴。
- 19 个错误码以枚举实现（`errors.py`），并提供 `from_state()` 显式映射，
  未知状态归入 `TOOL_ERROR`，不再返回字符串字面量。
- 进程内可重入锁与 `@serialized()` 装饰器（`lock.py`），重叠调用超时返回 `BUSY`。
- 审计日志 `argus/logs/actions.jsonl`，自动轮转。

**工程**
- 内联 7 个上游感知模块至 `argus/vendor/`（窗口截图、Vision OCR、基础布局解析等，
  来自 wechat-mac-rpa，MIT），版权与文件清单见 `NOTICE`。
- 三套单元测试共 66 条断言（`test_dedup.py` 12 条、`test_paginate.py` 22 条、
  `test_tools_mock.py` 32 条），全部通过，且均不依赖真实屏幕，可在无微信的机器上运行。
- `install.sh` 一键建 venv、装依赖、装 CLI 薄壳；`scripts/argus` 为启动入口。

### 开发期纠正的设计错误

这些问题在 1.0.0 发布前已修复，记录在此是因为它们解释了现版本行为为何如此设计：

- **回车发送假成功。** 早期实现按回车键（key code 36）发送，当微信「按 Enter 键
  发送消息」关闭时，回车只插入换行 —— 两条消息留在输入框里，函数却返回
  `success: true`。现改为 OCR 定位发送按钮并回读复核。
- **滚轮无效但接口返回成功。** 根因是滚轮事件按真实光标位置路由，
  `CGEventSetLocation` 只设置事件坐标、不移动光标。现先移动光标再发事件。
- **焦点落在左侧会话列表。** 逻辑点与物理像素混用导致点击坐标偏移，
  滚轮被会话列表吃掉。现坐标换算集中处理。
- **方向校准被误判为"滚动无效"。** 校准只滚 1 行，跨不过一条消息的高度；
  现改用正常步长并放大重试。
- **拼接顺序错误导致整段重复。** `k=0` 分支先合并再回退重试会污染累积结果，
  现先算重叠再决定合并。
- **锚点精确比较必然失败。** OCR 会在字符间插空格、把 `S.s` 读成 `S.S`、
  在句首多读一个 `9`；现改用骨架归一化 + 相似度匹配。
- **`_normalize` 漏处理省略号后跟句点**（`⋯.`），尾部残留句点导致前缀匹配
  永远失败。现补 `rstrip(".")`。
- **大跨度滚动会跳回更早内容。** 实测 `-30` / `-50` 行行为异常，
  现固定安全步长 `SAFE_SCROLL_LINES=8`。

### 已知边界（1.0.0 不解决）

1. **非文本消息不可见。** 图片、表情、语音、文件不产生 OCR 文本，读到的条数会少于
   肉眼所见。
2. **没有真实消息 ID。** `message_id` 是哈希 + 窗口内出现序号构造的。同一屏内能区分
   连发的相同内容；跨屏、且内容与时间戳完全一致的两条仍可能碰撞。
3. **昵称被界面本身截断**（显示为「某某物流：天⋯.」），全名不可还原。
4. **单窗口串行。** 屏幕、鼠标、剪贴板各只有一份，多会话只能排队，不能并发。
5. **发送要求微信在前台。** 实测 `NSRunningApplication.activate`、`open -b`、
   `osascript activate` 均无法稳定把微信切到前台；对照实验中计算器同样失败。
   现策略是明确拒绝而非硬闯。
6. **搜一搜面板无法用程序关闭。** 该面板没有可用的关闭控件，只能检测并返回错误。

### 验证环境与实测数据

验证环境：macOS 26.5.1、微信 4.1.15、Python 3.13.12、Apple Silicon；
窗口 772×640（截图 1544×1280）。

| 项目 | 实测值 |
|---|---|
| 截图（`screencapture -l`） | 约 130 ms |
| OCR（Apple Vision，34–59 元素） | 约 600 ms |
| 布局解析 | 约 380 ms |
| 单次完整感知 | 约 1.1–1.5 s |
| 单次发送（点击 + 粘贴 + 复核） | 约 2.5 s |
| 跨屏回溯 12 屏 | 14 条消息、0 缺口、无重复、约 42 s |

上述数字来自单机单次测量，用于量级参考，不作为性能承诺。

### 出处

感知层基础实现来自 [wechat-mac-rpa](https://github.com/wq19901103wq/wechat-mac-rpa)（MIT），
Argus 在其上重做了接口层、增量模型、回溯拼接、投递验证与安全策略。详见 `NOTICE`。

---

[Unreleased]: https://github.com/newdwp1985-eng/argus/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/newdwp1985-eng/argus/releases/tag/v1.0.1
[1.0.0]: https://github.com/newdwp1985-eng/argus/releases/tag/v1.0.0
