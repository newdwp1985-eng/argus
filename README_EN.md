# Argus

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)
![Tests](https://img.shields.io/badge/tests-94%20passed-brightgreen)
![Skill Market](https://img.shields.io/badge/skill%20market-pending%20review-blue)

**Vision RPA that reads the screen when the accessibility tree gives you nothing.**

WeChat 4.x, Douyin, and most Qt/CEF apps expose little more than a window frame through
macOS Accessibility. Argus takes the other road: screenshot → on-device OCR → reconstruct
message structure → window-relative actions. Every action comes back with a receipt, and it
only reports what it actually saw.

**Skill market status**: first submitted 2026-11-15 and rejected — the sandbox evaluation
did not pass (graded low quality). 1.0.1 (2026-11-18) addresses the cause: missing
dependencies now return an error code instead of crashing, and a zero-dependency
`argus self-test` provides evidence that can be checked on any machine. Status: pending
resubmission. Once approved, this section becomes the listing link.

```
$ARGUS latest -n 3
{
  "chat_name": "a photo group (178)",
  "messages": [
    {"sender": "someone", "text": "估计还是辐射头吧", "timestamp": "15:29",
     "message_id": "9b0f614f903d1df0:0"}
  ]
}
```

---

## Why this exists

On macOS 26 with WeChat 4.1.15, two widely used open-source approaches hit the same wall.
Measured on this machine (macOS 26.5.1, Apple Silicon):

| Project | Approach | Result |
|---|---|---|
| WeChat-MCP | Accessibility tree | AX tree collapsed to 3 `AXButton` nodes; 0 sessions read |
| wxauto-mac | Accessibility tree | hardcoded `"WeChat"` title match fails on the Chinese title |
| wechat-mac-rpa | Screenshot + Vision OCR | 59 OCR elements in 607 ms — works |

The first two are not badly written; the channel they depend on is gone on this OS version.
That is a routing problem, not a code-quality problem.

Argus continues along the third road and adds the part that comes after "it runs":

- **Verifiable sends** — read back the input box, compare content, locate the 发送 button by OCR, re-check afterwards;
- **Full-history reads** — scroll back across screens with skeleton anchors, report gaps instead of pretending continuity;
- **Real deduplication** — watermark + occurrence index, so two identical messages sent back-to-back are not collapsed into one;
- **Serialized access** — a process-wide re-entrant lock; overlapping calls get `BUSY` instead of corrupting each other.

---

## Three things worth a closer look

**1. Delivery is not collapsed into a boolean.**

A send has more than two outcomes, so `send_message` returns two independent fields:
`sent` (the send button was clicked and the input box verified empty) and
`confirmed_in_ui` (the text was actually read back from the message area). `sent: true`
with `confirmed_in_ui: false` means the action ran but the UI evidence is missing — Argus
returns a `warnings` entry asking for a human check, treating it as neither success nor
failure. A real failure (button not found, input box not cleared) returns
`SEND_UNVERIFIED` and never carries `sent: true`.

This was forced by a real incident: an early version pressed Enter to send, but with
WeChat's "press Enter to send" turned off, Enter only inserts a newline. The message sat in
the input box as two lines while the function returned `success: true`.

**2. Uncertainty is explicit.**

`monitor` returns an `uncertainty_note`: a successful poll is not real-time delivery.
`history --deep` returns `gaps` and `reached_top`. It never pretends to be continuous.

**3. Coordinates live in config, not code.**

Every ratio is in `argus/profile_default.json`. New monitor scaling, a different font size,
a resized window — edit JSON, not source.

---

## Install

```bash
git clone https://github.com/newdwp1985-eng/argus.git
cd argus
python -m argus.cli self-test    # step 0: verify the code — no deps, no WeChat
./install.sh
$ARGUS doctor                    # step 1: platform, dependencies, WeChat state
$ARGUS status                    # step 2: window and UI usable?
```

Step 0 is deliberately first: `self-test` runs pure-Python logic only, so it completes on a
machine without pyobjc, without WeChat, and even without macOS. It answers "is the code
correct" before the later steps answer "is the environment enough".

Grant **Screen Recording** (screenshots and OCR) and **Accessibility** (clicks) in
System Settings → Privacy & Security, then restart your terminal.

### Prerequisites come in three layers, each independently verifiable

| Layer | Needs | Verify with | Does not need |
|---|---|---|---|
| L1 logic | Python ≥ 3.10 | `python -m argus.cli self-test` | macOS, WeChat, any third-party package |
| L2 perception | macOS + pyobjc/Pillow/numpy/scipy + Screen Recording | `$ARGUS doctor` | a logged-in WeChat |
| L3 end-to-end | WeChat logged in + Accessibility + WeChat frontmost | `$ARGUS status` / `list` | — |

When dependencies are missing, all nine tools return `success: false` with
`DEPENDENCY_MISSING`; on non-macOS they return `PLATFORM_UNSUPPORTED`. No tracebacks.
That makes "cannot run here" and "the code is wrong" separable by `error_code` alone.

### As an MCP server

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

Add it to `~/.workbuddy/mcp.json` (or `~/.codebuddy/.mcp.json`) and trust the server in the
connector panel. Nine tools become available.

---

## Nine tools

| Tool | Purpose | Touches UI |
|---|---|---|
| `get_wechat_status` | Run state, window bounds, UI availability | no |
| `get_chat_list` | Recent conversations | no |
| `search_contact` | Contact matching with candidate list | no |
| `get_chat_history` | History; `deep=true` scrolls back | yes (switch) |
| `get_latest_messages` | Latest messages in current chat | no |
| `monitor_new_messages` | Incremental polling with watermark dedup | no |
| `send_message` | Send text; requires `confirm=true` | yes |
| `get_screenshot` | Save window screenshot | no |
| `get_ocr_result` | All OCR elements with coordinates | no |

Every tool returns the same envelope:
`success` / `operation_id` / `data` / `error` / `error_code` / `timestamp` / `warnings`.

---

## Safety model

Defaults are conservative because this touches real conversations.

| Mechanism | Behaviour |
|---|---|
| Send authorization | Requires explicit `confirm=true`, else `NOT_CONFIRMED` |
| Identity check | Refuses to send when the contact is not uniquely identified; returns candidates |
| Content check | Rejects empty, over-2000-char, and duplicate-within-30s |
| Foreground check | Refuses to send unless WeChat is frontmost — never pastes into another app |
| Send verification | Reads back the input box; `confirmed_in_ui` records the UI evidence |
| Concurrency | Process-wide re-entrant lock; overlapping calls get `BUSY` |
| Audit | Every action appended to `argus/logs/actions.jsonl` with rotation |

It does **not**: read the WeChat database, decrypt protocols, inject into processes, or bypass
login and screen lock.

---

## Measured

macOS 26.5.1 / WeChat 4.1.15 / Apple Silicon, window 772×640 (screenshot 1544×1280):

| Step | Time |
|---|---|
| Capture (`screencapture -l`) | ~130 ms |
| OCR (Vision, 34–59 elements) | ~600 ms |
| Layout parse | ~380 ms |
| Full perception | ~1.1–1.5 s |
| Send (click + paste + verify) | ~2.5 s |
| Scroll back 12 screens | 14 messages, 0 gaps, no duplicates, ~42 s |

---

## Known limitations

1. **Non-text messages are invisible.** Images, stickers, voice, files produce no OCR text; counts will be lower than what you see.
2. **No real message IDs.** `message_id` is a hash plus an in-window occurrence index. Duplicate-content messages across screens can still collide.
3. **Nicknames are truncated by the UI itself** (`某某物流：天⋯.`). Full names are not recoverable.
4. **Single window, serialized.** One screen, one mouse, one clipboard — customers queue up.
5. **WeChat must be frontmost to send.** `NSRunningApplication.activate`, `open -b`, and `osascript activate` all reported success while the foreground app did not change; a control test with Calculator failed the same way. Platform limit, not a bypassable bug.
6. **The 搜一搜 panel cannot be closed programmatically.** It has no usable close control; Argus detects it and returns an error instead.

For #5 the response is an explicit refusal, not a brute-force retry: better to return
`WECHAT_NOT_FRONTMOST` and ask the user to switch windows than to paste a customer message
into someone else's input box.

---

## Architecture

```
cli.py / mcp_server.py     two entry points, one shared tools.py
        │
        ├── tools.py       9 tools + safety policy + serialization
        │     ├── dedup.py      watermark-based incremental dedup (message_id)
        │     ├── paginate.py   cross-screen scroll-back (direction self-calibration
        │     │                 + skeleton-anchor merging)
        │     └── msg_parse.py  OCR elements → structured messages (ink luminance
        │                       separates nickname from body)
        │
        ├── core.py        windows / capture / coordinate mapping / input / UI state
        │     └── vendor/  capture, OCR, base layout parsing (from wechat-mac-rpa)
        │
        ├── config.py      externalized config (profile_default.json)
        ├── env.py         environment probe: platform/deps/missing (stdlib only)
        ├── selftest.py    zero-dependency self-test suite
        ├── errors.py      21 error codes as an enum, no string literals
        ├── lock.py        process-wide re-entrant mutex
        └── log.py         audit log (JSONL, rotating)
```

The judgement layer (`dedup` / `paginate` / `msg_parse`) is pure logic and fully unit-tested;
only `core` touches the system. That boundary keeps "changing policy" and "changing actions"
independent.

A second boundary separates "can it run here" from "does it run correctly": `env` reports
whether prerequisites hold, `core.__init__` turns that verdict into an exception via
`require_env()`, and the tool decorator turns the exception into a `DEPENDENCY_MISSING`
error code. On a machine without pyobjc, `import argus`, `doctor`, `self-test` and all four
test files still work; only calls that genuinely need the screen are refused.

---

## Tests

```bash
python tests/test_dedup.py         # watermark dedup, 12 cases
python tests/test_paginate.py      # scroll-back merging, 22 cases (gaps, duplicates, OCR noise)
python tests/test_tools_mock.py    # tool layer, 32 cases (contact matching, send guards, locking, config)
python tests/test_env.py           # degradation, 28 cases (missing deps, non-macOS, CLI doctor, self-test)

python -m argus.cli self-test      # packaged equivalent: 14 assertions, zero dependencies
```

None of the four touches a real screen, and none requires pyobjc: Apple frameworks and the
scientific stack are imported on demand. Measured in a venv with no third-party packages:
all 94 assertions pass.

---

## Credits

The perception layer (window capture, Vision OCR, base layout parsing) comes from
[wechat-mac-rpa](https://github.com/wq19901103wq/wechat-mac-rpa) (MIT). Argus rebuilds the
interface layer, incremental model, scroll-back merging, delivery verification, and safety
policy on top of it.

See [NOTICE](NOTICE) for the full attribution.

## License

MIT. See [LICENSE](LICENSE). Version history and known boundaries: [CHANGELOG.md](CHANGELOG.md).
