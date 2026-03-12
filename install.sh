#!/bin/bash
# screen-mcp 安装脚本 —— macOS / Linux
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
PLATFORM="$(uname -s)"

echo "════════════════════════════════════"
echo "  screen-mcp 安装脚本"
echo "  平台: $PLATFORM"
echo "════════════════════════════════════"

# ── 1. 检查 Python ────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] 未找到 python3"
    if [ "$PLATFORM" = "Darwin" ]; then
        echo "       请安装: brew install python3"
    else
        echo "       请安装: sudo apt install python3 python3-venv"
    fi
    exit 1
fi
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "[✓] Python $PY_VER"

# ── 2. 创建虚拟环境 ───────────────────────────────────────────────────────────
if [ ! -d "$VENV" ]; then
    echo "[...] 创建虚拟环境..."
    python3 -m venv "$VENV"
fi
echo "[✓] 虚拟环境: $VENV"

# ── 3. 安装 Python 依赖 ───────────────────────────────────────────────────────
echo "[...] 安装依赖..."
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q "mcp[cli]>=1.0.0" Pillow

# OCR（可选）
if command -v tesseract &>/dev/null; then
    "$VENV/bin/pip" install -q pytesseract
    echo "[✓] Tesseract OCR 已安装并启用"
else
    echo "[i] Tesseract 未安装（可选），macOS 使用 Accessibility API，无需 OCR"
    if [ "$PLATFORM" = "Darwin" ]; then
        echo "    如需 OCR 支持: brew install tesseract tesseract-lang"
    else
        echo "    如需 OCR 支持: sudo apt install tesseract-ocr tesseract-ocr-chi-sim"
    fi
fi
echo "[✓] Python 依赖安装完成"

# ── 4. Linux 额外工具 ─────────────────────────────────────────────────────────
if [ "$PLATFORM" = "Linux" ]; then
    echo "[Linux] 检查截图工具..."
    if ! command -v scrot &>/dev/null && ! command -v gnome-screenshot &>/dev/null; then
        echo "[!] 未找到截图工具，请安装: sudo apt install scrot"
    else
        echo "[✓] 截图工具已就绪"
    fi
fi

# ── 5. macOS 权限提示 ─────────────────────────────────────────────────────────
if [ "$PLATFORM" = "Darwin" ]; then
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  [重要] macOS 需要授权屏幕录制"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  系统设置 → 隐私与安全 → 屏幕录制"
    echo "  添加并勾选: Terminal / iTerm2 / 你用的终端"
    echo ""
fi

# ── 6. 生成 Claude Desktop 配置片段 ──────────────────────────────────────────
PYTHON_PATH="$VENV/bin/python"
SERVER_PATH="$SCRIPT_DIR/server.py"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  将以下配置添加到 Claude Desktop 配置文件"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
cat <<EOF
{
  "mcpServers": {
    "screen-assistant": {
      "command": "$PYTHON_PATH",
      "args": ["$SERVER_PATH"]
    }
  }
}
EOF
echo ""

# 自动写入配置文件
if [ "$PLATFORM" = "Darwin" ]; then
    CONFIG_PATH="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
elif [ "$PLATFORM" = "Linux" ]; then
    CONFIG_PATH="$HOME/.config/claude/claude_desktop_config.json"
fi

if [ -n "$CONFIG_PATH" ]; then
    echo "Claude Desktop 配置文件路径:"
    echo "  $CONFIG_PATH"
    echo ""
    read -p "是否自动写入配置？(y/N): " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        mkdir -p "$(dirname "$CONFIG_PATH")"
        # 如果配置文件已存在，备份
        if [ -f "$CONFIG_PATH" ]; then
            cp "$CONFIG_PATH" "$CONFIG_PATH.backup"
            echo "[✓] 已备份原配置到 $CONFIG_PATH.backup"
            # 用 python 合并 JSON（避免覆盖其他 MCP 服务器）
            "$VENV/bin/python" -c "
import json, os
config_path = '$CONFIG_PATH'
new_entry = {
    'screen-assistant': {
        'command': '$PYTHON_PATH',
        'args': ['$SERVER_PATH']
    }
}
with open(config_path) as f:
    config = json.load(f)
config.setdefault('mcpServers', {}).update(new_entry)
with open(config_path, 'w') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
print('[✓] 配置已合并写入')
"
        else
            cat > "$CONFIG_PATH" <<EOF2
{
  "mcpServers": {
    "screen-assistant": {
      "command": "$PYTHON_PATH",
      "args": ["$SERVER_PATH"]
    }
  }
}
EOF2
            echo "[✓] 配置文件已创建"
        fi
        echo ""
        echo "重启 Claude Desktop 即可生效 ✓"
    fi
fi

echo ""
echo "════════════════════════════════════"
echo "  安装完成！"
echo "  测试命令: $PYTHON_PATH $SERVER_PATH"
echo "════════════════════════════════════"
