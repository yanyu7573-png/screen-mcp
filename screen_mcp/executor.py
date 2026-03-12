"""
ActionExecutor — AI 自主操控鼠标键盘
安全第一：三级权限，Esc 紧急停止，移到左上角自动停止
"""
from __future__ import annotations
import time, threading, sys
from typing import Optional, Callable
from dataclasses import dataclass, field
from enum import Enum

# ── 安全开关 ──────────────────────────────────────────────────────────────────
try:
    import pyautogui
    pyautogui.FAILSAFE = True      # 鼠标移到屏幕左上角自动停止
    pyautogui.PAUSE    = 0.05      # 每次操作后停顿 50ms，防止过快
    _PYAUTOGUI_OK = True
except ImportError:
    _PYAUTOGUI_OK = False
    print("[executor] pyautogui 未安装: pip install pyautogui", file=sys.stderr)


# ── 权限级别 ──────────────────────────────────────────────────────────────────
class Level(Enum):
    AUTO    = 1   # 自动执行（滚动、折叠、复制）
    CONFIRM = 2   # 3s 倒计时确认（点击、保存、运行）
    MANUAL  = 3   # 强制手动确认（删除、终端命令、git push）


@dataclass
class Action:
    type:        str            # click / type / key / scroll / move
    target:      str            # 描述（供日志和 UI 显示）
    x:           Optional[int]  = None
    y:           Optional[int]  = None
    text:        Optional[str]  = None
    keys:        Optional[list] = None
    level:       Level          = Level.CONFIRM
    scroll_dir:  str            = "down"
    scroll_n:    int            = 3


@dataclass
class Task:
    description: str
    actions:     list[Action]    = field(default_factory=list)
    step:        int             = 0
    done:        bool            = False
    cancelled:   bool            = False
    results:     list[str]       = field(default_factory=list)


# ── 危险操作关键词 → 自动升级到 Level 3 ──────────────────────────────────────
_DANGER_KEYWORDS = [
    "delete","remove","rm ","drop","truncate","format",
    "git push","git commit","git reset",
    "send","submit","post","publish",
    "sudo","chmod","chown",
    "password","credential",
]

def _classify(action: Action) -> Level:
    """自动判断操作风险等级"""
    desc = (action.target + (action.text or "") + str(action.keys or "")).lower()
    if any(k in desc for k in _DANGER_KEYWORDS):
        return Level.MANUAL
    if action.type in ("scroll", "move"):
        return Level.AUTO
    return action.level


# ── 主执行器 ─────────────────────────────────────────────────────────────────
class ActionExecutor:
    """
    安全地执行 AI 规划的动作序列。
    - Level.AUTO:    直接执行
    - Level.CONFIRM: 回调通知 UI 显示倒计时，3s 后执行
    - Level.MANUAL:  回调通知 UI 显示确认弹窗，等用户响应
    """

    def __init__(self):
        self._stop_event   = threading.Event()
        self._current_task: Optional[Task] = None
        self._lock         = threading.Lock()

        # 回调钩子（由 Overlay 注册）
        self.on_step_preview:  Optional[Callable] = None   # (task, action, level) → None
        self.on_step_done:     Optional[Callable] = None   # (task, action, ok)    → None
        self.on_task_complete: Optional[Callable] = None   # (task)                → None
        self.on_confirm_needed:Optional[Callable] = None   # (task, action, cb)    → None  cb(True/False)

    # ── 公开 API ──────────────────────────────────────────────────────────────

    def run_task(self, task: Task, confirm_fn: Optional[Callable] = None):
        """在后台线程执行整个任务"""
        with self._lock:
            if self._current_task and not self._current_task.done:
                return False, "已有任务在执行"
            self._current_task = task
            self._stop_event.clear()

        threading.Thread(
            target=self._run, args=(task, confirm_fn),
            daemon=True, name="executor"
        ).start()
        return True, "已开始"

    def stop(self):
        """紧急停止当前任务"""
        self._stop_event.set()
        if self._current_task:
            self._current_task.cancelled = True
        print("[executor] 紧急停止", file=sys.stderr)

    def is_running(self) -> bool:
        return bool(
            self._current_task
            and not self._current_task.done
            and not self._current_task.cancelled
        )

    def current_task(self) -> Optional[Task]:
        return self._current_task

    # ── 内部执行循环 ──────────────────────────────────────────────────────────

    def _run(self, task: Task, confirm_fn: Optional[Callable]):
        for i, action in enumerate(task.actions):
            if self._stop_event.is_set() or task.cancelled:
                break

            task.step = i + 1
            level     = _classify(action)

            # 通知 UI 即将执行
            if self.on_step_preview:
                self.on_step_preview(task, action, level)

            if level == Level.AUTO:
                ok = self._execute(action)

            elif level == Level.CONFIRM:
                ok = self._confirm_countdown(task, action, confirm_fn)

            else:  # MANUAL
                ok = self._confirm_manual(task, action, confirm_fn)

            task.results.append(f"步骤{task.step}: {'✓' if ok else '✗'} {action.target}")

            if self.on_step_done:
                self.on_step_done(task, action, ok)

            if not ok and level != Level.AUTO:
                task.cancelled = True
                break

        task.done = True
        if self.on_task_complete:
            self.on_task_complete(task)

    def _confirm_countdown(self, task, action, confirm_fn, seconds=3) -> bool:
        """3s 倒计时，期间可取消"""
        confirmed = threading.Event()
        cancelled = threading.Event()

        def _cb(ok: bool):
            (confirmed if ok else cancelled).set()

        if self.on_confirm_needed:
            self.on_confirm_needed(task, action, _cb)
        elif confirm_fn:
            confirm_fn(task, action, _cb)

        deadline = time.time() + seconds
        while time.time() < deadline:
            if cancelled.is_set() or self._stop_event.is_set():
                return False
            if confirmed.is_set():
                break
            time.sleep(0.1)

        if self._stop_event.is_set():
            return False
        return self._execute(action)

    def _confirm_manual(self, task, action, confirm_fn) -> bool:
        """等待用户手动确认"""
        confirmed = threading.Event()
        cancelled = threading.Event()

        def _cb(ok: bool):
            (confirmed if ok else cancelled).set()

        if self.on_confirm_needed:
            self.on_confirm_needed(task, action, _cb)
        elif confirm_fn:
            confirm_fn(task, action, _cb)
        else:
            # 无 UI 时默认拒绝高危操作
            return False

        while not confirmed.is_set() and not cancelled.is_set():
            if self._stop_event.is_set():
                return False
            time.sleep(0.1)

        return confirmed.is_set()

    def _execute(self, action: Action) -> bool:
        """实际执行鼠标键盘操作"""
        if not _PYAUTOGUI_OK:
            print(f"[executor] pyautogui 不可用，跳过: {action.target}", file=sys.stderr)
            return False
        try:
            if action.type == "click":
                if action.x and action.y:
                    pyautogui.click(action.x, action.y)
                else:
                    print(f"[executor] click 缺少坐标: {action.target}", file=sys.stderr)
                    return False

            elif action.type == "double_click":
                pyautogui.doubleClick(action.x, action.y)

            elif action.type == "type":
                if action.text:
                    pyautogui.typewrite(action.text, interval=0.03)

            elif action.type == "hotkey":
                if action.keys:
                    pyautogui.hotkey(*action.keys)

            elif action.type == "key":
                if action.keys:
                    for k in action.keys:
                        pyautogui.press(k)

            elif action.type == "scroll":
                clicks = action.scroll_n if action.scroll_dir == "down" else -action.scroll_n
                if action.x and action.y:
                    pyautogui.scroll(clicks, x=action.x, y=action.y)
                else:
                    pyautogui.scroll(clicks)

            elif action.type == "move":
                if action.x and action.y:
                    pyautogui.moveTo(action.x, action.y, duration=0.3)

            elif action.type == "drag":
                # action.text 格式: "x2,y2"
                if action.x and action.y and action.text:
                    x2, y2 = map(int, action.text.split(","))
                    pyautogui.dragTo(x2, y2, duration=0.4, button="left")

            print(f"[executor] ✓ {action.type}: {action.target}", file=sys.stderr)
            return True

        except pyautogui.FailSafeException:
            print("[executor] 触发安全锁（鼠标移到左上角），停止所有操作", file=sys.stderr)
            self.stop()
            return False
        except Exception as e:
            print(f"[executor] ✗ {action.type} 失败: {e}", file=sys.stderr)
            return False


# ── 单例 ──────────────────────────────────────────────────────────────────────
executor = ActionExecutor()
