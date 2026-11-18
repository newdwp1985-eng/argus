#!/usr/bin/env bash
# Argus installer — macOS only.
#
# Creates an isolated venv, installs dependencies, and links a thin CLI wrapper.
# Idempotent: safe to re-run after pulling new code.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"
BIN_DIR="${ARGUS_BIN_DIR:-$HOME/.local/bin}"
PY_MIN="3.10"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# --- 1. 平台检查 ---------------------------------------------------------
[ "$(uname -s)" = "Darwin" ] || die "Argus 目前只支持 macOS（当前系统：$(uname -s)）。"

if ! command -v python3 >/dev/null 2>&1; then
  die "找不到 python3。请先安装 Python $PY_MIN+（brew install python@3.11）。"
fi

PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c "
import sys
if sys.version_info < (3, 10):
    sys.exit('Argus 需要 Python 3.10+，当前是 $PY_VER')
" || die "Python 版本过低：$PY_VER"

say "Python $PY_VER ✓"

# --- 2. 虚拟环境 ---------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
  say "创建虚拟环境 → $VENV"
  python3 -m venv "$VENV"
fi

say "安装依赖（首次约需 1-3 分钟）"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$ROOT/requirements.txt"
say "依赖安装完成 ✓"

# --- 3. 自检 -------------------------------------------------------------
# 先跑零依赖自检（纯逻辑），再确认工具层能加载。两者的失败含义不同：
# 前者挂了是代码问题，后者挂了是依赖问题，分开报比只报「导入失败」有用。
if "$VENV/bin/python" -m argus.cli self-test >/dev/null 2>&1; then
  say "纯逻辑自检通过 ✓（14 项断言）"
else
  die "纯逻辑自检失败。请执行 '$VENV/bin/python -m argus.cli self-test' 查看是哪一项。"
fi

if "$VENV/bin/python" -c "
import sys; sys.path.insert(0, '$ROOT')
import argus
assert len(argus.TOOLS) == 9, '工具数异常'
" 2>/dev/null; then
  say "包导入自检通过 ✓（9 个工具）"
else
  die "工具层加载失败。请执行 '$VENV/bin/python -m argus.cli doctor' 查看缺哪一项依赖。"
fi

if "$VENV/bin/python" -m argus.cli doctor --strict --no-runtime >/dev/null 2>&1; then
  say "环境诊断通过 ✓"
else
  warn "环境诊断未通过。执行 'argus doctor' 查看 missing 与 install_hint。"
fi

# --- 4. CLI 薄壳 ---------------------------------------------------------
mkdir -p "$BIN_DIR"
ln -sf "$ROOT/scripts/argus" "$BIN_DIR/argus"
say "CLI 已链接 → $BIN_DIR/argus"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "提示：$BIN_DIR 不在 PATH 中。把它加进 shell 配置后即可直接用 argus 命令。" ;;
esac

# --- 5. 权限提示 ---------------------------------------------------------
cat <<'TIP'

接下来需要授予两项系统权限（只授予一次，后续不再询问）：

  系统设置 → 隐私与安全性 → 屏幕录制     ← 截图与 OCR 必需
  系统设置 → 隐私与安全性 → 辅助功能     ← 点击与键盘操作需要

授予后需重启终端进程才会生效。

验证：argus status

TIP
