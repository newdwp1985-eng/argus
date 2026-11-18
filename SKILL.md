---
name: argus
display_name: Argus 百眼 · 视觉 RPA
display_name_en: Argus
slug: argus
description: "当界面的无障碍树读不出内容时，改用截图 + 本地 OCR 把界面读出来，再按窗口坐标操作，每一步都带回执。专治微信 4.x、抖音、Qt/CEF 这类 AX 树只剩几个空按钮的窗口。凡是需要读某个人的聊天记录、看群里有没有新消息、在微信里发一段话、监听新消息、识别谁说了什么、对窗口做 OCR、按坐标点一下的任务都先用本技能。触发词：微信自动化、读聊天记录、发微信消息、监听新消息、群消息识别、桌面 OCR、视觉 RPA、坐标点击、computer use、微信 4.x 自动化。不适用于：读取微信数据库、解密通信协议、注入进程、绕过登录或锁屏。"
description_zh: "当界面的无障碍树读不出内容时，改用截图 + 本地 OCR 把界面读出来，再按窗口坐标操作，每一步都带回执。专治微信 4.x、抖音、Qt/CEF 这类 AX 树只剩几个空按钮的窗口。凡是需要读某个人的聊天记录、看群里有没有新消息、在微信里发一段话、监听新消息、识别谁说了什么、对窗口做 OCR、按坐标点一下的任务都先用本技能。"
description_en: "When an app's accessibility tree collapses to a few empty buttons, read it with screenshots plus on-device OCR instead, then act by window-relative coordinates — every action returns a receipt. Built for WeChat 4.x, Douyin, and Qt/CEF surfaces on macOS. Use it to read someone's chat history, check a group for new messages, send a WeChat message, monitor incoming messages, attribute who said what, OCR any window, or click a point. Not for: reading WeChat databases, decrypting protocols, injecting into processes, or bypassing login or screen lock."
version: 1.0.1
author: 波弟
category: 系统与桌面自动化
tags: [wechat, ocr, rpa, macos, automation, vision, mcp, accessibility, vision-fallback, chat]
platforms:
  - macos
permission:
  - 屏幕录制（截图与 OCR 必需）
  - 辅助功能 / UI 自动化（点击与键盘输入）
  - 剪贴板读写（粘贴消息内容）
  - 本机窗口枚举与前台状态读取
allowed-tools: Bash
agent_created: true
---

# Argus · 百眼

一句话：**当界面读不出结构时，用像素把它读出来，并且只报告自己真正看见的东西。**

---

## 运行前提（先看这一节）

Argus 操作的是真实窗口，因此有硬性前提。前提分三层，每层都能独立验证，不必一次到位：

| 层级 | 需要什么 | 验证命令 | 不需要什么 |
|---|---|---|---|
| L1 纯逻辑 | Python ≥ 3.10 | `$ARGUS self-test` | 不需要 macOS、不需要微信、不需要任何第三方库 |
| L2 感知层 | macOS + pyobjc/Pillow/numpy/scipy + 屏幕录制权限 | `$ARGUS doctor` | 不需要微信登录 |
| L3 端到端 | 微信已登录 + 辅助功能权限 + 微信在前台 | `$ARGUS status`、`$ARGUS list` | — |

`$ARGUS doctor` 在任何环境都能跑完：`ready` 说明 L2 是否齐备，`missing` 列出缺哪几样，
`problems` 用人话解释原因，`install_hint` 给出安装命令。它**不会**因为缺依赖而中途报错退出。

`$ARGUS self-test` 跑的是不碰屏幕的纯逻辑断言（错误码、配置、增量去重、跨屏拼接、
并发闸门、降级路径），全绿说明代码本身没问题。**在没有 GUI 的沙箱或 CI 里，这是唯一
能拿到的有效证据** —— L2/L3 无法在无屏幕环境验证，原因不是缺陷，而是视觉 RPA 的输入
本来就是屏幕。

缺依赖时不需要猜：所有工具都会返回 `success: false` 并给出 `DEPENDENCY_MISSING` 或
`PLATFORM_UNSUPPORTED`，不会抛 traceback。

---

## 什么时候用

**用**：要在微信里读消息、发消息、看有没有新消息；要 OCR 某个窗口；要按窗口坐标点一下。
**不用**：要批量导出聊天记录、要读本地数据库、要接管登录态 —— 这些都不做，也不要尝试绕。

---

## 第一步：三条命令确认环境

```bash
$ARGUS doctor        # 依赖、平台、微信运行状态，一次说清
$ARGUS status        # 微信在跑吗？窗口多大？界面可用吗？
$ARGUS list          # 最近会话列表
```

`status` 返回 `ui_status: ok` 才继续。若不是 `ok`，照 `ui_detail` 说的做，不要重试刷屏。

---

## 命令表

| 想做什么 | 命令 |
|---|---|
| 看环境（不需要屏幕） | `$ARGUS doctor` |
| 跑纯逻辑自检（不需要屏幕） | `$ARGUS self-test` |
| 看微信状态 | `$ARGUS status` |
| 看会话列表 | `$ARGUS list` |
| 找联系人 | `$ARGUS search "张三"` |
| 读某人聊天记录 | `$ARGUS history "张三" --limit 20` |
| 读更早的历史（跨屏回溯） | `$ARGUS history "张三" --deep --scrolls 8` |
| 看当前聊天最新消息 | `$ARGUS latest --limit 10` |
| 抽一次样看有没有新消息 | `$ARGUS monitor --once` |
| 连续盯 N 秒 | `$ARGUS monitor --seconds 60` |
| 发消息 | `$ARGUS send "张三" "内容" --confirm` |
| 截图 | `$ARGUS shot` |
| 看窗口 OCR 明细 | `$ARGUS ocr` |

所有命令返回同一形状的 JSON：`success` / `operation_id` / `data` / `error` / `error_code` / `timestamp` / `warnings`。

---

## 五条铁律

**1. 发送必须由人授权。**
`send` 不带 `--confirm` 一定被拒（`NOT_CONFIRMED`）。不要为了"帮用户省一步"自己加上这个参数。
用户说"发吧"算授权；用户说"你觉得该怎么回"不算。

**2. 身份不确定就不发。**
`search` 返回多个候选、或 `send` 回 `CONTACT_NOT_CONFIRMED` 时，把候选列出来问用户挑，
不要自己选一个顺眼的。发错人的代价远大于多问一句。

**3. 微信必须在前台，否则拒绝发送。**
这是 macOS 26 的平台限制（实测三种激活 API 全部返回成功但前台不变），
不是可以绕过的 bug。拿到 `WECHAT_NOT_FRONTMOST` 就请用户把微信切到前台，不要反复重试。

**4. 不要把「输入框清空了」当成「消息发出去了」。**
`send` 返回里的 `confirmed_in_ui` 才是界面证据。`false` 时如实告诉用户"未能从界面确认，请人工核对"，
不要说"已发送"。

**5. 轮询成功 ≠ 实时送达。**
`monitor` 的 `uncertainty_note` 说明了漏读条件。转述给用户时保留这个不确定性，
不要说"我会实时盯着"。

---

## 输出怎么读

`success: true` 只代表**这次调用按预期完成了**，不代表业务目标达成。要看 `data` 里的具体字段：

- `send`：看 `confirmed_in_ui` 与 `delivery`
- `history --deep`：看 `pages_read`、`gaps`、`reached_top`
- `monitor`：看 `new_messages` 里每条带 `message_id`，重复调用不会重复上报
- `search`：看 `candidates`

`success: false` 时，`error_code` 是给程序看的：

| 错误码 | 含义 | 该怎么办 |
|---|---|---|
| `NOT_CONFIRMED` | 没拿到人工授权 | 问用户 |
| `CONTACT_NOT_FOUND` | 会话列表里没这个人 | 换关键词，或让用户先打开一次对话框 |
| `AMBIGUOUS` | 命中多个同名 | 列出候选问用户 |
| `CONTACT_NOT_CONFIRMED` | 打开的会话不是目标 | 同上，绝不盲发 |
| `WECHAT_NOT_FRONTMOST` | 微信不在前台 | 请用户切到前台 |
| `SEARCH_PANEL_OPEN` | 搜一搜面板挤占聊天区 | 请用户关掉该面板 |
| `DUPLICATE_BLOCKED` | 30 秒内发过同样内容 | 确实要重发就等窗口过去 |
| `BUSY` | 另一个操作正在占用界面 | 等它结束，不要并发硬闯 |
| `DEPENDENCY_MISSING` | 缺 pyobjc / Pillow / numpy / scipy | 按 `$ARGUS doctor` 的 `install_hint` 装齐 |
| `PLATFORM_UNSUPPORTED` | 当前不是 macOS | 换 macOS；逻辑层可用 `$ARGUS self-test` 验证 |
| `WECHAT_NOT_RUNNING` | 微信没启动 | 启动并登录 |

---

## 已知限制（如实告知用户，不要粉饰）

1. **非文本消息不可见**：图片、表情、语音、文件不产生 OCR 文本，读到的条数会少于肉眼所见。
2. **昵称会被 UI 截断**：会话列表本身就显示成「某某物流：天⋯.」，全名读不出来。
3. **单窗口串行**：同一时刻只能操作一个窗口，多客户需要排队。
4. **跨屏重复文本可能碰撞**：没有微信真实消息 ID，`message_id` 是「哈希 + 出现序号」构造的。
5. **搜一搜面板关不掉**：该面板无可用关闭控件，只能检测并报错。

---

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 所有工具都返回 `DEPENDENCY_MISSING` | 依赖没装齐 | `$ARGUS doctor` 看 `missing`，按 `install_hint` 装 |
| 所有工具都返回 `PLATFORM_UNSUPPORTED` | 不在 macOS 上 | 换 macOS；先跑 `$ARGUS self-test` 确认逻辑没问题 |
| `WECHAT_NOT_RUNNING` | 微信没启动 | 启动并登录 |
| `ui_status: not_ready` | 界面被遮挡/最小化/锁定 | 按 `ui_detail` 恢复 |
| OCR 结果为空 | 窗口太小或内容全在侧栏 | 把窗口拉到至少 600×480 |
| 发送总回 `SEND_UNVERIFIED` | 输入框识别区域偏了 | 调 `profile_default.json` 的 `layout.input_box` |
| `cliclick 未安装` 警告 | 可选依赖缺失 | 忽略（已降级为 CGEvent），或 `brew install cliclick` |

配置在 `argus/profile_default.json`，可用环境变量 `ARGUS_CONFIG` 指向自己的副本。
日志在 `argus/logs/`，数据在 `argus/data/`，两者都可用 `ARGUS_LOG_DIR` / `ARGUS_DATA_DIR` 改到别处。

---

## 作为 MCP 服务

```bash
python -m argus.mcp_server        # stdio
```

注册到 WorkBuddy：在 `~/.workbuddy/mcp.json` 的 `mcpServers` 下加

```json
{
  "argus": {
    "command": "/path/to/python",
    "args": ["-m", "argus.mcp_server"],
    "env": { "PYTHONPATH": "/path/to/argus" }
  }
}
```

保存后在连接器管理页对 `argus` 点「信任」才会激活。
