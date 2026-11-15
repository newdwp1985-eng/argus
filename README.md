# Argus · 百眼

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)
![Tests](https://img.shields.io/badge/tests-66%20passed-brightgreen)
![Skill Market](https://img.shields.io/badge/skill%20market-pending%20review-blue)

**读得懂屏幕的视觉 RPA —— 专治无障碍树失效的桌面应用。**

当微信 4.x、抖音、多数 Qt/CEF 界面的无障碍树只剩几个空按钮时，Argus 换成另一条路：
截图 → 本地 OCR → 还原消息结构 → 窗口相对坐标操作。每一步都带回执，只报告自己真正看见的东西。

> Vision RPA for apps that refuse to expose their UI.

```
$ARGUS latest -n 3
{
  "chat_name": "某个摄影交流群（178）",
  "messages": [
    {"sender": "某人", "text": "早期估计还是辐射头吧", "timestamp": "15:29",
     "message_id": "9b0f614f903d1df0:0"}
  ]
}
```

**技能市场状态**：2026-11-15 已提交 WorkBuddy 技能市场审核。审核通过后此处替换为详情页链接与一键安装方式；未通过则按反馈整改后重新提交。技能包与仓库同源，仅少了 README 与 `CHANGELOG.md`。

---

## 为什么会有这个项目

2026 年的 macOS 上，主流微信自动化方案踩到了同一堵墙。以下是在本机（macOS 26.5.1 / 微信 4.1.15 / Apple Silicon）的实际结果：

| 方案 | 技术路线 | 本机实测结果 |
|---|---|---|
| WeChat-MCP | Accessibility AX 树 | AX 树只剩 3 个 `AXButton`，会话读取 0 条；作者已在 issue 中确认与 macOS 26 相关 |
| wxauto-mac | Accessibility AX 树 | 窗口标题硬编码 `"WeChat"`，中文标题「微信」匹配失败 |
| wechat-mac-rpa | 截图 + Vision OCR | 读到 59 个 OCR 元素 / 607 ms，可用 |

结论不是"前两个写得差"，而是 **它们赖以为生的通道在这个系统版本上已经不可用**。这是路线问题，不是代码质量问题。

Argus 接着第三条路继续往下做，补的是"能跑起来之后怎么不出错"：

- 发送不再"按下回车就算成功"，而是**回读输入框 → 比对内容 → OCR 定位发送按钮 → 发送后复核**；
- 长会话不再"只能看一屏"，而是**跨屏回溯 + 重叠锚点拼接 + 缺口显式上报**；
- 监听不再"靠哈希猜重复"，而是**水位线 + 出现序号**，同一个人连发两条「好的」不会被吞掉；
- 并发不再靠调用方自觉，而是**进程内串行闸门**，重叠调用明确返回 `BUSY` 而不是互相打断。

---

## 三个不是卖点但值得看的地方

**1. 投递结果不折叠成一个布尔值。**

一次发送的结局不只是成功或失败，所以 `send_message` 返回两个独立字段：
`sent`（点击发送按钮 + 输入框已清空校验通过）与 `confirmed_in_ui`（发送后在消息区
真正读到了这条内容）。前者为真、后者为假时，说明动作执行了但界面证据缺失 ——
Argus 返回 `warnings` 要求人工核对，既不当作成功，也不当作失败。真正的失败
（找不到发送按钮、输入框未清空）返回 `SEND_UNVERIFIED`，不会带 `sent: true`。

这个设计是被事故逼出来的：早期版本用回车键发送，而微信「按 Enter 键发送消息」关闭时
回车只插入换行 —— 消息留在输入框里变成了两行，函数却返回 `success: true`。

**2. 不确定性显式化。**

`monitor` 会告诉你 `uncertainty_note`：轮询成功不等于实时送达，两次轮询之间被挤出可见区的
消息会漏读。`history --deep` 会告诉你 `gaps` 和 `reached_top`：哪里可能有跳页，有没有到底。
它不会假装自己是连续的。

**3. 坐标从配置读，不写死在代码里。**

所有比例阈值在 `argus/profile_default.json`。换显示器缩放、改微信字号、拉大窗口，
改 JSON 即可，不用改源码。

---

## 快速开始

```bash
git clone https://github.com/newdwp1985-eng/argus.git
cd argus
./install.sh                    # 建 venv、装依赖、装 CLI 薄壳
$ARGUS status                   # 确认环境
```

首次运行需要在「系统设置 → 隐私与安全性」中授予**屏幕录制**权限（截图与 OCR 必需）。
点击类操作需要**辅助功能**权限。

### 作为 MCP 服务

```json
{
  "mcpServers": {
    "argus": {
      "command": "/path/to/argus/.venv/bin/python",
      "args": ["-m", "argus.mcp_server"],
      "env": { "PYTHONPATH": "/path/to/argus" }
    }
  }
}
```

写进 `~/.workbuddy/mcp.json`（或 `~/.codebuddy/.mcp.json`），重启后在连接器管理页对
`argus` 点「信任」。9 个工具即刻可用。

---

## 九个工具

| 工具 | 作用 | 会动界面吗 |
|---|---|---|
| `get_wechat_status` | 运行状态、窗口边界、UI 可用性 | 否 |
| `get_chat_list` | 会话列表（昵称 / 预览 / 未读 / 时间） | 否 |
| `search_contact` | 联系人匹配，多义时返回候选 | 否 |
| `get_chat_history` | 聊天记录，`deep=true` 时跨屏回溯 | 是（切会话） |
| `get_latest_messages` | 当前聊天最新消息 | 否 |
| `monitor_new_messages` | 增量监听，水位线去重 | 否 |
| `send_message` | 发送文字，必须 `confirm=true` | 是 |
| `get_screenshot` | 保存窗口截图 | 否 |
| `get_ocr_result` | 输出全部 OCR 元素与坐标 | 否 |

统一返回结构：

```json
{
  "success": true,
  "operation_id": "op-71b62246670b",
  "data": { },
  "error": null,
  "error_code": null,
  "timestamp": 1763264000.0,
  "warnings": []
}
```

---

## 安全模型

这个项目的默认值都偏保守，因为它会碰真实的对话框。

| 机制 | 行为 |
|---|---|
| 发送授权 | 必须显式 `confirm=true`，否则 `NOT_CONFIRMED` |
| 身份确认 | 联系人无法唯一确定时拒绝发送，返回候选 |
| 内容校验 | 空消息、超过 2000 字、30 秒内重复内容均拒绝 |
| 前台校验 | 微信不在前台则拒绝发送，绝不把内容粘贴到别的应用 |
| 发送复核 | 发送后回读输入框确认已清空，`confirmed_in_ui` 记录界面证据 |
| 并发保护 | 进程内可重入锁，重叠调用返回 `BUSY` |
| 审计 | 所有动作写入 `argus/logs/actions.jsonl`（自动轮转） |

它**不会**做这些事：读取微信数据库、解密通信协议、注入进程、绕过登录或锁屏。

---

## 实测数据

本机 macOS 26.5.1 / 微信 4.1.15 / Apple Silicon，窗口 772×640（截图 1544×1280）：

| 项目 | 数字 |
|---|---|
| 截图（`screencapture -l`） | 约 130 ms |
| OCR（Apple Vision，34–59 元素） | 约 600 ms |
| 布局解析 | 约 380 ms |
| 单次完整感知 | 约 1.1–1.5 s |
| 单次发送（点击 + 粘贴 + 复核） | 约 2.5 s |
| 跨屏回溯 12 屏 | 14 条消息、0 缺口、无重复、约 42 s |

跨屏回溯的 `reached_top` 判定：连续两次滚动无位移才算到顶，单次无位移可能是事件丢失。

---

## 已知限制

诚实列出，因为它们会影响能不能用：

1. **非文本消息不可见。** 图片、表情、语音、文件不产生 OCR 文本，读到的条数会少于肉眼所见。
2. **没有真实消息 ID。** `message_id` 是「哈希 + 窗口内出现序号」构造的。同一屏内能区分连发的相同内容，跨屏且内容与时间戳完全一致的两条仍可能碰撞。
3. **昵称被 UI 截断。** 会话列表本身显示为「某某物流：天⋯.」，全名读不出来。
4. **单窗口串行。** 屏幕、鼠标、剪贴板都只有一份，多客户只能排队，不能并发。
5. **微信必须在前台才能发送。** 实测 `NSRunningApplication.activate`、`open -b`、`osascript activate` 在本进程调用均无法稳定把微信切到前台，对照实验中连计算器同样失败 —— 这是平台限制，不是可以绕过的 bug。
6. **搜一搜面板关不掉。** 该面板没有可用的关闭控件，只能检测并返回错误。

第 5 条的应对是明确的拒绝而不是硬闯：宁可返回 `WECHAT_NOT_FRONTMOST` 让用户切一下窗口，
也不把客户消息粘贴到别人的输入框里。

---

## 架构

```
cli.py / mcp_server.py     两个入口，共用 tools.py
        │
        ├── tools.py       9 个工具 + 安全策略 + 串行闸门
        │     ├── dedup.py      水位线增量去重（message_id）
        │     ├── paginate.py   跨屏回溯（方向自校准 + 骨架锚点拼接）
        │     └── msg_parse.py  OCR 元素 → 结构化消息（墨色分昵称/正文）
        │
        ├── core.py        窗口 / 截图 / 坐标换算 / 输入动作 / UI 状态机
        │     └── vendor/  截图、OCR、基础布局解析（来自 wechat-mac-rpa）
        │
        ├── config.py      配置外置（profile_default.json）
        ├── errors.py      错误码枚举（19 个，不再用字符串字面量）
        ├── lock.py        进程内可重入互斥
        └── log.py         审计日志（JSONL，自动轮转）
```

判断层与通道层是分开的：`dedup` / `paginate` / `msg_parse` 都是纯逻辑，可被单测完全钉住；
`core` 才碰系统。这条边界让"改判断"和"改操作"互不牵连。

---

## 测试

```bash
python tests/test_dedup.py         # 水位线去重 12 项
python tests/test_paginate.py      # 跨屏拼接 22 项（含跳页、重复、OCR 噪声场景）
python tests/test_tools_mock.py    # 工具层 32 项（联系人匹配、发送校验、并发、配置）
```

三套测试均不碰真机，可在无微信的机器上跑完。真机验证需要微信已登录并授予屏幕录制权限。

---

## 出处

感知层的基础实现（窗口截图、Vision OCR、布局解析）来自
[wechat-mac-rpa](https://github.com/wq19901103wq/wechat-mac-rpa)（MIT）。
Argus 在它之上重做了接口层、增量模型、回溯拼接、投递验证与安全策略。

详见 [NOTICE](NOTICE)。

## 许可

MIT。见 [LICENSE](LICENSE)。版本变更与已知边界见 [CHANGELOG.md](CHANGELOG.md)。
