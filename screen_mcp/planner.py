"""
TaskPlanner — 把自然语言任务规划成动作序列
使用 Claude API 分析截图，返回结构化的动作列表
"""
from __future__ import annotations
import sys, base64, json, time
from typing import Optional
from .executor import Action, Task, Level


# ── 提示词 ────────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """你是一个 macOS 自动化助手，根据屏幕截图和用户任务，规划精确的鼠标键盘操作序列。

返回 JSON 格式，示例：
{
  "steps": [
    {
      "type": "click",
      "target": "点击 Run 按钮",
      "x": 823,
      "y": 456,
      "level": 2
    },
    {
      "type": "hotkey",
      "target": "保存文件",
      "keys": ["command", "s"],
      "level": 2
    },
    {
      "type": "type",
      "target": "输入修复后的代码",
      "text": "data?.map(item => item.id)",
      "level": 1
    }
  ],
  "estimated_seconds": 5,
  "description": "点击 Run，等待结果，保存"
}

操作类型：click / double_click / type / hotkey / key / scroll / move / drag
风险等级：1=自动执行  2=3秒确认  3=强制手动确认（用于删除/git push/终端命令）

规则：
- 坐标必须是截图中可见元素的实际位置
- 删除/发送/git操作必须设 level=3
- 如果任务无法完成，返回 {"error": "原因"}
"""


class TaskPlanner:
    """
    接收截图 + 任务描述 → 返回 Task 对象（含动作序列）
    """

    def __init__(self):
        self._client = None
        self._available = False

    def init(self) -> bool:
        try:
            import anthropic
            self._client    = anthropic.Anthropic()
            self._available = True
            return True
        except ImportError:
            print("[planner] anthropic 未安装", file=sys.stderr)
            return False
        except Exception as e:
            print(f"[planner] 初始化失败: {e}", file=sys.stderr)
            return False

    def plan(self, task_desc: str, screenshot_b64: str,
             screen_context: str = "") -> Optional[Task]:
        """
        根据截图规划任务动作序列。
        返回 Task 对象，失败返回 None。
        """
        if not self._available and not self.init():
            return None

        # 构建 Vision 请求
        messages = [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type":       "base64",
                        "media_type": "image/jpeg",
                        "data":       screenshot_b64,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        f"当前屏幕信息：{screen_context[:500]}\n\n"
                        f"用户任务：{task_desc}\n\n"
                        "请规划操作步骤，返回 JSON："
                    ),
                },
            ],
        }]

        try:
            resp = self._client.messages.create(
                model      = "claude-sonnet-4-5-20251022",   # Vision 能力
                max_tokens = 800,
                system     = SYSTEM_PROMPT,
                messages   = messages,
            )
            raw = resp.content[0].text.strip()

            # 提取 JSON（可能有 markdown 包裹）
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]

            data = json.loads(raw)

            if "error" in data:
                print(f"[planner] AI 拒绝任务: {data['error']}", file=sys.stderr)
                return None

            actions = []
            for step in data.get("steps", []):
                actions.append(Action(
                    type       = step.get("type","click"),
                    target     = step.get("target",""),
                    x          = step.get("x"),
                    y          = step.get("y"),
                    text       = step.get("text"),
                    keys       = step.get("keys"),
                    level      = Level(step.get("level", 2)),
                    scroll_dir = step.get("scroll_dir","down"),
                    scroll_n   = step.get("scroll_n", 3),
                ))

            return Task(
                description = task_desc,
                actions     = actions,
            )

        except json.JSONDecodeError as e:
            print(f"[planner] JSON 解析失败: {e}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"[planner] 规划失败: {e}", file=sys.stderr)
            return None


planner = TaskPlanner()
