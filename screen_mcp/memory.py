"""
Memory v2 — 会话内存
修复：
  - [PERF] timeline/errors/insights 列表截断改用 deque，O(1) 而非 O(n) 重建
  - [BUG]  topics dict 中 list.append 并发安全改为 deque
  - [CLEAN] _fmt_time 移除每次调用的 import datetime
"""
from __future__ import annotations
import time, re, datetime
from collections import defaultdict, deque
from typing import Optional


# ── 任务模式识别 ───────────────────────────────────────────────────────────────

PATTERNS = {
    "coding":    ["Visual Studio Code","Xcode","PyCharm","Cursor","vim","neovim","Emacs","Sublime Text"],
    "debugging": ["Terminal","iTerm2","iTerm","Warp","Alacritty","kitty"],
    "research":  ["Google Chrome","Safari","Arc","Firefox","Microsoft Edge","Brave Browser"],
    "writing":   ["Pages","Word","wpsoffice","Notion","Typora","Obsidian","Bear"],
    "reading":   ["Preview","Skim","PDF Expert"],
    "meeting":   ["Zoom","Teams","Google Meet","FaceTime","Lark","Slack","Discord"],
    "design":    ["Figma","Sketch","Photoshop","Illustrator","Affinity"],
}

def detect_pattern(app: str, url: str = "") -> str:
    for pattern, apps in PATTERNS.items():
        if any(a.lower() in app.lower() for a in apps):
            return pattern
    if url:
        if any(k in url for k in ["github.com","stackoverflow.com","docs.","developer.","mdn.","devdocs."]):
            return "research"
        if any(k in url for k in ["youtube.com","netflix.com","bilibili.com","twitch.tv"]):
            return "entertainment"
        if any(k in url for k in ["figma.com","canva.com"]):
            return "design"
    return "general"


# ── 会话内存 ──────────────────────────────────────────────────────────────────

class SessionMemory:
    """
    追踪本次会话：App 时长、切换序列、任务模式、错误历史、AI 洞察、话题聚类。
    使用 deque 替代 list + 手动截断，O(1) 插入/删除。
    """

    def __init__(self):
        self.start_time        = time.time()
        self.app_time: dict[str, float]     = defaultdict(float)
        self.timeline          = deque(maxlen=100)   # FIX: deque 替代 list[-100:]
        self.errors            = deque(maxlen=30)    # FIX: deque 替代 list[-30:]
        self.insights          = deque(maxlen=50)    # FIX: deque 替代 list[-50:]
        # FIX: topics 字典本身需要上限，原来只限 value deque 的大小，dict key 仍无限增长
        self.topics: dict[str, deque] = {}
        self._topics_max = 80  # 最多追踪 80 个域名/App，超出丢弃最老的
        self._last_app         = ""
        self._last_switch      = time.time()
        self._current_pattern  = "general"

    # ── 记录 ──────────────────────────────────────────────────────────────────

    def on_app_switch(self, app: str, title: str = "", url: str = "",
                      text_preview: str = ""):
        now = time.time()
        if self._last_app:
            self.app_time[self._last_app] += now - self._last_switch

        self._last_app           = app
        self._last_switch        = now
        self._current_pattern    = detect_pattern(app, url)

        self.timeline.append({
            "ts":      now,
            "time":    _fmt_time(now),
            "type":    "switch",
            "app":     app,
            "title":   title[:60],
            "url":     url,
            "pattern": self._current_pattern,
            "preview": text_preview[:200],
        })

        key = _domain(url) if url else app
        if key:
            if key not in self.topics:
                # FIX: 超出上限时丢弃最早加入的 key
                if len(self.topics) >= self._topics_max:
                    self.topics.pop(next(iter(self.topics)))
                self.topics[key] = deque(maxlen=10)
            self.topics[key].append(title[:40])

    def on_content_change(self, app: str, text_preview: str = ""):
        now = time.time()
        self.timeline.append({
            "ts": now, "time": _fmt_time(now),
            "type": "change", "app": app, "preview": text_preview[:200],
        })

    def on_error(self, app: str, errors: list[str]):
        now = time.time()
        self.errors.append({
            "ts": now, "time": _fmt_time(now),
            "app": app, "errors": errors[:5],
        })

    def add_insight(self, text: str):
        self.insights.append(f"[{_fmt_time(time.time())}] {text}")

    # ── 查询 ──────────────────────────────────────────────────────────────────

    def current_pattern(self) -> str:
        return self._current_pattern

    def app_summary(self) -> list[dict]:
        now   = time.time()
        total = dict(self.app_time)
        if self._last_app:
            total[self._last_app] = total.get(self._last_app, 0) + (now - self._last_switch)
        result = sorted(
            [{"app": k, "minutes": round(v / 60, 1)} for k, v in total.items()],
            key=lambda x: x["minutes"], reverse=True,
        )
        return result[:10]

    def recent_timeline(self, n: int = 10) -> list[dict]:
        items = list(self.timeline)
        return items[-n:]

    def recent_errors(self, n: int = 5) -> list[dict]:
        items = list(self.errors)
        return items[-n:]

    def idle_warning(self, threshold_min: float = 5.0) -> Optional[dict]:
        if not self.timeline:
            return None
        last    = self.timeline[-1]
        elapsed = (time.time() - last["ts"]) / 60
        if elapsed >= threshold_min:
            return {
                "app":     last.get("app",""),
                "minutes": round(elapsed, 1),
                "title":   last.get("title",""),
                "hint":    f"用户已在 {last.get('app','')} 停留 {elapsed:.1f} 分钟，可能需要帮助",
            }
        return None

    def topic_summary(self) -> dict:
        return {k: list(v)[-3:] for k, v in list(self.topics.items())[:8]}

    def session_narrative(self) -> str:
        now      = time.time()
        duration = round((now - self.start_time) / 60, 1)
        lines    = [f"## 本次会话（已持续 {duration} 分钟）\n"]

        apps = self.app_summary()
        if apps:
            lines.append("### 应用使用时长")
            for a in apps[:5]:
                lines.append(f"- {a['app']}: {a['minutes']} 分钟")

        lines.append("\n### 最近活动轨迹")
        for e in self.recent_timeline(8):
            icon = "📱" if e["type"] == "switch" else "🔄"
            line = f"- [{e['time']}] {icon} {e['app']}"
            if e.get("title"):   line += f" — {e['title']}"
            if e.get("pattern") and e["pattern"] != "general":
                line += f" [{e['pattern']}]"
            lines.append(line)

        lines.append(f"\n### 当前任务模式: **{self._current_pattern}**")

        recent_errs = self.recent_errors(3)
        if recent_errs:
            lines.append("\n### ⚠️ 近期错误")
            for e in recent_errs:
                lines.append(f"- [{e['time']}] {e['app']}: {e['errors'][0][:80]}")

        insights_list = list(self.insights)
        if insights_list:
            lines.append("\n### 🤖 AI 洞察记录")
            for i in insights_list[-3:]:
                lines.append(f"- {i}")

        topics = self.topic_summary()
        if topics:
            lines.append("\n### 话题关联")
            for domain, titles in list(topics.items())[:4]:
                lines.append(f"- **{domain}**: {', '.join(str(t) for t in titles[:2])}")

        idle = self.idle_warning(5.0)
        if idle:
            lines.append(f"\n### ⏰ 注意\n{idle['hint']}")

        return "\n".join(lines)


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _fmt_time(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")  # FIX: 不再每次 import

def _domain(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url)
    return m.group(1) if m else ""
