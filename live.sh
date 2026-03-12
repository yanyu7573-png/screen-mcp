#!/bin/bash
# screen-mcp 实时监控快捷脚本
# 用法: ./live.sh [start|stop|status]
# 在新终端窗口中运行，让 AI 持续感知你的屏幕

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="/tmp/screen-mcp-live.pid"

case "${1:-start}" in
  start)
    echo "正在启动实时监控..."
    echo "Claude 将持续感知你的屏幕，直到你运行 ./live.sh stop"
    echo ""
    # 在后台运行（通过 MCP 的 start_live_monitor 工具触发才是正确方式）
    # 此脚本仅供直接测试用
    "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/server.py" &
    echo $! > "$PID_FILE"
    echo "PID: $!"
    ;;
  stop)
    if [ -f "$PID_FILE" ]; then
      kill "$(cat "$PID_FILE")" 2>/dev/null
      rm "$PID_FILE"
      echo "已停止"
    else
      echo "未运行"
    fi
    ;;
  status)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "运行中 (PID: $(cat "$PID_FILE"))"
    else
      echo "未运行"
    fi
    ;;
  *)
    echo "用法: $0 [start|stop|status]"
    ;;
esac
