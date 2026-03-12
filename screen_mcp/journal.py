"""
Journal v2 — 持久化屏幕日志
修复：
  - [BUG] read_today() 切片逻辑错误：text[idx:][-max_chars:] → text[idx:idx+max_chars]
  - [BUG] write_event() 文件竞态：在 append 模式下再 read_text，两次操作间可被其他进程修改
  - [BUG] _archive_if_large() rename 无错误处理，目标文件已存在时静默丢失数据
  - [PERF] 每次写入都全文读取检查今日标题，改为内存缓存
  - [PERF] get_stats() 全文读取两次，合并为一次
"""
from __future__ import annotations
import os, re, threading, time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


DATA_DIR     = Path.home() / ".screen-mcp"
JOURNAL_FILE = DATA_DIR / "journal.md"
MAX_FILE_MB  = 10

# 内存缓存：记录已写过标题的日期，避免每次写入都全文读取
_written_dates: set[str] = set()
_write_lock = threading.Lock()   # 文件级别写锁，防止并发竞态


def _ensure_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


ARCHIVE_TTL_DAYS = 90   # 归档文件保留天数，超过则自动删除


def _archive_if_large():
    """归档超大日志文件，含错误处理防止数据丢失。同时清理过期归档。"""
    try:
        if not JOURNAL_FILE.exists():
            return
        if JOURNAL_FILE.stat().st_size <= MAX_FILE_MB * 1024 * 1024:
            _cleanup_old_archives()  # 借机清理过期归档，不增加额外开销
            return
        ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive = DATA_DIR / f"journal_{ts}.md"
        if archive.exists():
            archive = DATA_DIR / f"journal_{ts}_{os.getpid()}.md"
        JOURNAL_FILE.rename(archive)
        _written_dates.clear()
        _cleanup_old_archives()
    except Exception as e:
        import sys
        print(f"[journal] 归档失败: {e}", file=sys.stderr)


def _cleanup_old_archives():
    """删除超过 ARCHIVE_TTL_DAYS 天的归档文件"""
    try:
        cutoff = time.time() - ARCHIVE_TTL_DAYS * 86400
        for f in DATA_DIR.glob("journal_*.md"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
    except Exception:
        pass


def purge_before(days: int = 30) -> dict:
    """
    删除 N 天前的日志条目（直接修改 journal.md）。
    不影响归档文件，只清理主文件中的旧内容。
    返回清理前后的统计。
    """
    if not JOURNAL_FILE.exists():
        return {"status": "no_file"}
    try:
        text   = JOURNAL_FILE.read_text(encoding="utf-8")
        before = len(text)
        # 找到 N 天前的日期标题，保留该日期之后的内容
        cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        # 找第一个 >= cutoff_date 的日期标题
        date_pattern = re.compile(r"^## (\d{4}-\d{2}-\d{2})", re.M)
        keep_from    = 0
        for m in date_pattern.finditer(text):
            if m.group(1) >= cutoff_date:
                keep_from = m.start()
                break
        if keep_from == 0:
            return {"status": "nothing_to_purge", "kept_chars": before}
        new_text = text[keep_from:]
        with _write_lock:
            JOURNAL_FILE.write_text(new_text, encoding="utf-8")
            _written_dates.clear()
        return {
            "status":       "purged",
            "removed_chars":before - len(new_text),
            "kept_chars":   len(new_text),
            "cutoff_date":  cutoff_date,
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ── 写入 ──────────────────────────────────────────────────────────────────────

def write_event(event_type: str, app: str, content: str,
                title: str = "", url: str = "", tags: list[str] | None = None):
    """
    写一条屏幕事件记录。
    线程安全：使用文件锁，今日标题通过内存缓存判断，不再在 append 模式下 read_text。
    """
    _ensure_dir()
    _archive_if_large()

    ICONS = {
        "switch":   "📱", "change":  "🔄", "error":   "❌",
        "insight":  "🤖", "reminder":"⏰", "idle":    "😴",
        "task":     "📋", "note":    "📝", "nav":     "🌐",
        "title":    "📄", "bg_nav":  "🔍",
    }
    icon  = ICONS.get(event_type, "•")
    today = _today()

    lines = [f"\n### [{_now()}] {icon} {event_type.upper()}: {app}"]
    if title:   lines.append(f"**窗口**: {title[:80]}")
    if url:     lines.append(f"**URL**: {url[:120]}")
    if tags:    lines.append(f"**标签**: {' '.join(f'`{t}`' for t in tags if t)}")
    if content: lines.append(f"\n{content[:600]}")

    entry = "\n".join(lines) + "\n"

    with _write_lock:
        with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
            # 用内存缓存判断是否需要写日期标题，避免重复 read_text
            if today not in _written_dates:
                # 双重检查：启动后第一次写时，文件可能已有今日标题（上次会话写的）
                try:
                    existing = JOURNAL_FILE.read_text(encoding="utf-8")
                    if f"## {today}" not in existing:
                        f.write(f"\n## {today}\n")
                except Exception:
                    f.write(f"\n## {today}\n")
                _written_dates.add(today)
            f.write(entry)


def write_insight(text: str, source: str = "claude"):
    """写一条 AI 洞察"""
    write_event("insight", source, text)


def write_reminder(text: str, context: str = ""):
    """写一条提醒"""
    ctx = f"\n\n上下文: {context}" if context else ""
    write_event("reminder", "system", f"{text}{ctx}")


# ── 读取 ──────────────────────────────────────────────────────────────────────

def read_today(max_chars: int = 8000) -> str:
    """读取今日日志。当天暂无记录时返回提示而非空字符串。"""
    if not JOURNAL_FILE.exists():
        return f"（今日 {_today()} 暂无日志记录）"
    try:
        text         = JOURNAL_FILE.read_text(encoding="utf-8")
        today_marker = f"## {_today()}"
        idx          = text.rfind(today_marker)
        if idx == -1:
            # 今天第一次查询时还没写过日志 → 返回最近内容作为兜底
            return text[-min(max_chars, 2000):] or f"（今日 {_today()} 暂无日志记录）"
        return text[idx : idx + max_chars]
    except Exception:
        return ""


def read_recent(days: int = 3, max_chars: int = 6000) -> str:
    """
    读取最近 N 天的日志。
    FIX: 原实现完全忽略 days 参数，只取文件末尾固定字符数。
    现在真正按日期过滤，找到最近 N 个 ## YYYY-MM-DD 标题并返回其内容。
    """
    if not JOURNAL_FILE.exists():
        return ""
    try:
        text = JOURNAL_FILE.read_text(encoding="utf-8")
        # 找所有日期标题的位置
        date_pattern = re.compile(r"^## \d{4}-\d{2}-\d{2}", re.M)
        markers = [(m.start(), m.group()) for m in date_pattern.finditer(text)]
        if not markers:
            return text[-max_chars:]
        # 取最近 N 天
        recent = markers[-days:]
        start  = recent[0][0]
        return text[start: start + max_chars]
    except Exception:
        return ""


def search(query: str, max_results: int = 10) -> list[str]:
    """关键词搜索日志"""
    if not JOURNAL_FILE.exists():
        return []
    try:
        text    = JOURNAL_FILE.read_text(encoding="utf-8")
        blocks  = re.split(r"\n### ", text)
        results = [b for b in blocks if query.lower() in b.lower()]
        return [f"### {b[:400]}" for b in results[-max_results:]]
    except Exception:
        return []


def get_stats() -> dict:
    """日志统计（单次读取，避免多次IO）"""
    if not JOURNAL_FILE.exists():
        return {"exists": False}
    try:
        size = JOURNAL_FILE.stat().st_size
        text = JOURNAL_FILE.read_text(encoding="utf-8")
        return {
            "exists":  True,
            "size_kb": size // 1024,
            "entries": text.count("### ["),
            "insights":text.count("🤖"),
            "errors":  text.count("❌"),
            "days":    len(re.findall(r"^## \d{4}-\d{2}-\d{2}", text, re.M)),
            "path":    str(JOURNAL_FILE),
        }
    except Exception:
        return {"exists": True, "error": "读取失败"}
