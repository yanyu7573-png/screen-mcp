# screen-mcp — 让 AI 实时看到你的屏幕

> 无需截图，无需复制粘贴。Claude 直接"看到"你正在做什么，像一个坐在你身边的助手。

```
你：  "帮我看看这个报错"
Claude: 我看到你的终端有 ValueError：第42行把字符串传给了 int()，修复方式是...

你：  "这道题怎么做"
Claude: 我看到你在做递归题，这道题需要先写 base case...
```

---

## 快速安装（macOS）

```bash
# 1. 克隆项目
git clone https://github.com/yanyu1919/screen-mcp.git
cd screen-mcp

# 2. 创建虚拟环境并安装依赖
python3 -m venv .venv
.venv/bin/pip install "mcp[cli]" Pillow

# 3. S 模式 AI 主动思考（可选，需要 Anthropic API Key）
.venv/bin/pip install anthropic

# 4. 注册到 Claude Code
claude mcp add -s user screen-assistant \
  $(pwd)/.venv/bin/python \
  -- $(pwd)/server.py
```

**授权屏幕录制**：系统设置 → 隐私与安全性 → 屏幕录制 → 勾选 Terminal

---

## 四种感知模式

| 模式 | 触发条件 | CPU | 月费 | 适用场景 |
|------|---------|-----|------|---------|
| **off** | 无 | 0% | $0 | 完全关闭 |
| **E** Economy | App 切换 + URL 变化 | <0.5% | ~$1 | 日常挂着 |
| **A** Advanced | E + 内容哈希变化 | ~5% | ~$2 | 写代码/查资料 |
| **S** Supreme | A + AI 每10s主动思考 | ~10% | ~$20-30 | 深度工作 |

> E/A 模式不调用 AI API，$1/月 是你问 Claude 问题时消耗的 token 费。
> S 模式调用 claude-haiku-4-5，额外 ~$20-30/月（可用 `configure` 调整间隔）。

切换模式：
```
set_monitor_mode("E")   # 省电
set_monitor_mode("S")   # 全功率
set_monitor_mode("off") # 关闭
```

---

## S 模式配置（可选）

S 模式需要 Anthropic API Key：

```bash
# 写入 ~/.zshrc（永久生效）
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.zshrc
source ~/.zshrc
```

获取 Key：https://console.anthropic.com/settings/keys
查看费用：https://console.anthropic.com/usage

---

## 21 个工具

### 感知
| 工具 | 说明 |
|------|------|
| `get_screen_context` | **首选**：当前屏幕文字，自动查后台 Chrome |
| `get_screen_screenshot` | 全屏截图 |
| `get_monitor_screenshot` | 返回缓存截图（不重新截） |
| `get_live_context` | 实时监控摘要 + 操作轨迹 |
| `get_workflow_context` | 全景：屏幕 + 会话 + 今日日志 |

### 提取
| 工具 | 说明 |
|------|------|
| `get_full_content` | Select All+Copy，不受滚动限制 |
| `get_full_page_content` | 浏览器全页（优先 JS） |
| `scroll_and_capture` | 自动滚动拼成长图 |

### 窗口
| 工具 | 说明 |
|------|------|
| `capture_screen` | 手动截图（全屏/区域/多显示器） |
| `get_active_window_info` | 当前窗口名/标题/URL |
| `list_open_windows` | 所有打开的窗口 |
| `get_clipboard` | 剪贴板（自动脱敏密码） |
| `detect_screen_errors` | 扫描报错（Python/JS/HTTP等） |

### 监控
| 工具 | 说明 |
|------|------|
| `set_monitor_mode` | 切换 off/E/A/S 模式 |
| `start_live_monitor` | 快捷开启 |
| `stop_live_monitor` | 关闭 |
| `live_monitor_stats` | 状态 + 费用估算 |

### 记忆（跨会话）
| 工具 | 说明 |
|------|------|
| `record_insight` | 保存洞察到日志 |
| `get_journal` | 查历史（关键词/日期） |
| `get_session_timeline` | 今日时间线 + 行为分析 |
| `set_reminder` | 设置跨会话提醒 |
| `get_smart_insights` | 查 S 模式自动生成的洞察 |
| `clear_journal` | 清理旧日志，控制隐私 |

### 工具箱
| 工具 | 说明 |
|------|------|
| `ping` | 健康检查 + 版本 |
| `configure` | 运行时调参（间隔/TTL等） |
| `get_screen_diff` | 最近 N 分钟屏幕变化 |
| `export_session_report` | 生成日报/站会 markdown |
| `smart_search` | 跨源搜索（日志+会话+屏幕） |
| `get_app_context` | 某 App 最近 N 分钟完整上下文 |

---

## 斜杠命令

在 Claude Code 中直接输入：

| 命令 | 效果 |
|------|------|
| `/look` | 看当前屏幕 |
| `/monitor` | 开启 S 模式监控 |
| `/monitor-stop` | 关闭监控 |
| `/workflow` | AI 主动分析你在做什么 |
| `/insight` | 今日时间线分析 |

---

## 数据与隐私

- **截图**：仅存内存，5 分钟后自动清除，不上传
- **日志**：存在 `~/.screen-mcp/journal.md`，只在本地
- **脱敏**：自动过滤密码、Token、信用卡、JWT、SSH Key、URL 凭证等
- **黑名单**：1Password、Bitwarden 等密码管理器自动屏蔽
- **清理**：`clear_journal(days=30)` 删除旧日志

---

## Windows 安装

```bat
git clone https://github.com/yanyu1919/screen-mcp.git
cd screen-mcp
python -m venv .venv
.venv\Scripts\pip install "mcp[cli]" Pillow pywin32 psutil
```

注册 MCP：
```bat
claude mcp add -s user screen-assistant ^
  %cd%\.venv\Scripts\python.exe ^
  -- %cd%\server.py
```

---

## 系统要求

- Python 3.10+
- Claude Code（claude.ai/code）
- macOS：最佳支持，全功能
- Windows：基础功能（截图+窗口信息）
- Linux：基础功能（需要 scrot 或 gnome-screenshot）

---

## License

MIT
