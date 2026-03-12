#!/bin/bash
# screen-mcp 一键安装脚本
# 用法：curl -fsSL https://raw.githubusercontent.com/yanyu7573-png/screen-mcp/main/install.sh | bash

set -e

REPO="https://github.com/yanyu7573-png/screen-mcp"
INSTALL_DIR="$HOME/.screen-mcp-app"

echo "============================================"
echo "  screen-mcp 安装程序"
echo "  让 Claude AI 实时看到你的屏幕"
echo "============================================"
echo ""

# ── 检查依赖 ─────────────────────────────────────────────────────────────────

if ! command -v python3 &>/dev/null; then
    echo "[错误] 未找到 Python3，请先安装："
    echo "  macOS:   brew install python3"
    echo "  Ubuntu:  sudo apt install python3 python3-pip python3-venv"
    exit 1
fi

PYTHON_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
if [ "$PYTHON_MINOR" -lt 10 ]; then
    echo "[错误] 需要 Python 3.10+，当前版本过低"
    exit 1
fi

if ! command -v git &>/dev/null; then
    echo "[错误] 未找到 git，请先安装：brew install git"
    exit 1
fi

if ! command -v claude &>/dev/null; then
    echo "[错误] 未找到 Claude Code，请先安装："
    echo "  https://claude.ai/code"
    exit 1
fi

echo "[✓] 依赖检查通过"

# ── 下载代码 ─────────────────────────────────────────────────────────────────

if [ -d "$INSTALL_DIR/.git" ]; then
    echo "[*] 已有安装，更新到最新版本..."
    cd "$INSTALL_DIR" && git pull --quiet
else
    echo "[*] 下载 screen-mcp..."
    git clone --quiet "$REPO" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── 安装 Python 依赖 ─────────────────────────────────────────────────────────

echo "[*] 安装依赖..."
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet "mcp[cli]" Pillow

if [ -n "$ANTHROPIC_API_KEY" ]; then
    echo "[*] 检测到 ANTHROPIC_API_KEY，安装 S 模式 AI 思考模块..."
    .venv/bin/pip install --quiet anthropic
fi

echo "[✓] 依赖安装完成"

# ── 注册到 Claude Code ────────────────────────────────────────────────────────

echo "[*] 注册 MCP 服务器..."
PYTHON_PATH="$INSTALL_DIR/.venv/bin/python"
SERVER_PATH="$INSTALL_DIR/server.py"

# 先删旧的再注册（防止路径变更）
claude mcp remove screen-assistant -s user 2>/dev/null || true
claude mcp add -s user screen-assistant "$PYTHON_PATH" -- "$SERVER_PATH"

echo "[✓] MCP 注册完成"

# ── 安装斜杠命令 ─────────────────────────────────────────────────────────────

COMMANDS_DIR="$HOME/.claude/commands"
mkdir -p "$COMMANDS_DIR"

cat > "$COMMANDS_DIR/look.md" << 'EOF'
调用 get_screen_context 工具查看我当前屏幕上的内容，然后直接告诉我你看到了什么，不要问我任何问题。
EOF

cat > "$COMMANDS_DIR/monitor.md" << 'EOF'
调用 start_live_monitor 工具开启实时屏幕监控，然后简短告诉我监控已开启、探测间隔、以及我现在可以直接问你屏幕上的任何问题。
EOF

cat > "$COMMANDS_DIR/monitor-stop.md" << 'EOF'
调用 stop_live_monitor 工具停止实时屏幕监控，然后简短告诉我监控已停止和共记录了多少事件。
EOF

cat > "$COMMANDS_DIR/workflow.md" << 'EOF'
调用 get_workflow_context 工具获取我的工作流全景，然后根据返回的会话叙述、今日日志、当前屏幕状态，主动告诉我：你观察到我在做什么、遇到了哪些问题、有什么建议。不要问我问题，直接给出你的分析和见解。
EOF

cat > "$COMMANDS_DIR/insight.md" << 'EOF'
调用 get_session_timeline 工具获取本次会话的完整时间线和行为分析，然后告诉我：我今天在哪些 App 花了最多时间、主要在做什么任务、遇到了哪些错误、当前任务模式是什么。用简洁的中文总结。
EOF

echo "[✓] 斜杠命令安装完成"

# ── macOS 权限提示 ────────────────────────────────────────────────────────────

if [[ "$OSTYPE" == "darwin"* ]]; then
    echo ""
    echo "============================================"
    echo "  macOS 用户：需要授权屏幕录制权限"
    echo "============================================"
    echo "  系统设置 → 隐私与安全性 → 屏幕录制"
    echo "  → 勾选 Terminal（或你使用的终端 App）"
fi

# ── 完成 ─────────────────────────────────────────────────────────────────────

echo ""
echo "============================================"
echo "  安装完成！重新打开 Claude Code 即可使用"
echo "============================================"
echo ""
echo "  快速开始："
echo "    /look          查看当前屏幕"
echo "    /monitor       开启实时监控"
echo "    /workflow      AI 分析你在做什么"
echo "    /insight       今日使用分析"
echo ""
echo "  开启 S 模式（AI 主动思考）："
echo "    export ANTHROPIC_API_KEY='sk-ant-...'"
echo "    重新安装后在 Claude Code 说「开 S 模式」"
echo ""
echo "  项目地址：$REPO"
echo ""
