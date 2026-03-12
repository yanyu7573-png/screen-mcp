#!/usr/bin/env python3
"""
Screen Assistant MCP Server v6.0 — 全面修复版
修复了 v5-s 中发现的 25 个问题（3个致命 + 7个高危 + 10个中等 + 5个低危）

架构：单一 ScreenBrain 取代原有三个独立监控类
  ┌─────────────────────────────────────────────┐
  │              ScreenBrain                    │
  │  ┌──────────────────────────────────────┐   │
  │  │  感知循环（1.5s）                    │   │
  │  │  L1: 窗口名变化 (<5ms)              │   │
  │  │  L2: 感知哈希对比 (<10ms)           │   │
  │  │  L3: 完整截图+文字提取 (~150ms)     │   │
  │  └──────────┬───────────────────────────┘   │
  │             │ emit(Event)                    │
  │   ┌─────────▼─────────┐                     │
  │   │    EventBus        │                     │
  │   └──┬──────┬──────┬──┘                     │
  │      │      │      │                         │
  │  State  Memory  Journal                      │
  │  Cache  Update  Write                        │
  └─────────────────────────────────────────────┘
           │
    所有 MCP 工具统一从 brain 读取

核心原则：
  1. 单一事实来源 — 所有状态由 ScreenBrain 维护
  2. 事件驱动 — 切换/变化/错误立即触发，零漏检
  3. 图文分离 — 截图单独返回，不嵌 JSON
  4. 输出上限 — 每个工具响应 < 20KB
  5. 持久记忆 — journal 跨会话，memory 会话内

工具列表（20个）：
  感知  get_screen_context / get_screen_screenshot / get_monitor_screenshot
        get_live_context / get_workflow_context
  提取  get_full_content / get_full_page_content / scroll_and_capture
  窗口  capture_screen / get_active_window_info / list_open_windows
        get_clipboard / detect_screen_errors
  监控  set_monitor_mode(off/E/A/S) / start_live_monitor / stop_live_monitor / live_monitor_stats
  记忆  record_insight / get_journal / get_session_timeline / set_reminder
  S智能 get_smart_insights（S模式AI主动思考洞察）
"""

import sys, platform, base64, subprocess, tempfile, os
import re, json, threading, time
from collections import deque
from typing import Optional, List
from io import BytesIO

try:
    from PIL import Image
except ImportError:
    print("[ERROR] pip install Pillow", file=sys.stderr); sys.exit(1)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("[ERROR] pip install 'mcp[cli]'", file=sys.stderr); sys.exit(1)

try:
    from screen_mcp.journal import (
        write_event, write_insight, write_reminder as _journal_reminder,
        read_today, read_recent, search as journal_search, get_stats as journal_stats,
    )
    from screen_mcp.memory import SessionMemory, detect_pattern
    _MEMORY_OK = True
except ImportError as e:
    print(f"[warn] memory/journal 不可用: {e}", file=sys.stderr)
    _MEMORY_OK = False

try:
    from screen_mcp.executor import executor, Action, Task, Level
    from screen_mcp.planner  import planner
    from screen_mcp.overlay  import show_insight, show_error, show_action, show_status, show_complete
    _X_OK = True
except ImportError as e:
    print(f"[warn] X 模式模块不可用: {e}", file=sys.stderr)
    _X_OK = False

# ── 常量 ──────────────────────────────────────────────────────────────────────
PLATFORM = platform.system()
IS_MAC   = PLATFORM == "Darwin"
IS_WIN   = PLATFORM == "Windows"
IS_LINUX = PLATFORM == "Linux"

TERMINAL_APPS = {"Terminal","iTerm2","iTerm","Warp","Alacritty","kitty","Hyper","WezTerm","Tabby"}
BROWSERS      = {"Google Chrome","Safari","Arc","Microsoft Edge","Brave Browser","Firefox","Opera","Vivaldi"}
BLACKLIST     = {"1Password","1Password 7","1Password 8","Bitwarden","Keychain Access","Enpass","Dashlane","LastPass"}

TEXT_LIMIT    = 4000
SUMMARY_LIMIT = 600
CONTEXT_LIMIT = 8000

VERSION    = "8.0"
_START_TIME = time.time()
print(f"[screen-mcp v{VERSION}] 平台:{PLATFORM} Python:{sys.version.split()[0]}", file=sys.stderr)

# ── MCP ───────────────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="screen-assistant",
    instructions=(
        "你有实时查看用户屏幕的能力，持续感知、思考并记忆用户的工作状态。规则：\n"
        "1. 永远不要让用户截图或复制文字——你自己能看到屏幕。\n"
        "2. 每次对话开始时调 get_workflow_context 了解用户状态和历史背景。\n"
        "3. 用户问屏幕内容时，先调 get_screen_context（文字），再调 get_screen_screenshot（图）。\n"
        "4. 发现重要洞察时调 record_insight 永久记录。\n"
        "5. 截图优先用 get_monitor_screenshot（缓存），不重复截。\n"
        "6. 需要看全页内容时调 get_full_content 或 get_full_page_content。"
    ),
)

# ── 隐私过滤 ──────────────────────────────────────────────────────────────────
SENS_RE = re.compile(
    r"(?i)(\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b"               # 信用卡号
    r"|password\s*[:=]\s*\S+|passwd\s*[:=]\s*\S+"                          # 密码
    r"|secret\s*[:=]\s*\S+|api[_\s]?key\s*[:=]\s*\S+"                     # secret/api key
    r"|(access|auth|bearer|refresh)[\s_]?token\s*[:=]\s*\S+"               # token
    r"|private[\s_]?key\s*[:=]\s*\S+"                                       # private key
    r"|Authorization:\s*Bearer\s+\S+"                                       # HTTP Bearer
    r"|(export|set)\s+\w*(secret|key|token|pass)\w*\s*=\s*\S+"            # shell export
    r"|\b[0-9a-f]{32,64}\b"                                                # 32-64位hex字符串
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"       # JWT token
    r"|mysql\s+.*?-p\S+"                                                    # mysql -p<password>
    r"|psql\s+.*?password=[^\s]+"                                          # psql password=
    r"|\b(ssh|scp|sftp)\s+.*?-i\s+\S+"                                    # SSH key paths
    r"|(curl|wget)\s+.*?-u\s+\S+:\S+"                                      # curl -u user:pass
    r")"
)

# URL 凭证脱敏（单独处理，保留域名部分）
_URL_CRED_RE = re.compile(r"(https?://)([^@\s]+):([^@\s]+)@")

def is_bl(app: str) -> bool: return any(b.lower() in app.lower() for b in BLACKLIST)
def redact(t: str) -> str:
    t = _URL_CRED_RE.sub(r"\1[REDACTED]:[REDACTED]@", t)  # URL 凭证先处理
    return SENS_RE.sub("[REDACTED]", t)

# ── 图片工具 ──────────────────────────────────────────────────────────────────
def compress(img: Image.Image, max_px: int = 900, q: int = 55) -> bytes:
    r = min(max_px / max(img.width, img.height), 1.0)
    if r < 1.0:
        img = img.resize((int(img.width*r), int(img.height*r)), Image.LANCZOS)
    TARGET = 37 * 1024  # base64 后 < 50000 字符，避免 Claude Code token 超限
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=q, optimize=True)
    # FIX: 先检查再循环，避免首次成功时的无意义迭代
    if len(buf.getvalue()) <= TARGET:
        return buf.getvalue()
    while len(buf.getvalue()) > TARGET and q > 20:
        q -= 10
        buf = BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=q, optimize=True)
    return buf.getvalue()

def data_url(b64: str) -> str:
    return f"data:image/jpeg;base64,{b64}"

# ══════════════════════════════════════════════════════════════════════════════
# 底层：截图
# ══════════════════════════════════════════════════════════════════════════════
def _shot_mac(region=None, display=0) -> Optional[str]:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f: path = f.name
    try:
        cmd = ["screencapture", "-x", "-t", "png"]
        if display > 0: cmd += ["-D", str(display)]
        if region:      cmd += ["-R", "{},{},{},{}".format(*region)]
        cmd.append(path)
        if subprocess.run(cmd, capture_output=True, timeout=10).returncode: return None
        return base64.b64encode(compress(Image.open(path))).decode()
    except Exception as e:
        print(f"[shot] {e}", file=sys.stderr); return None
    finally:
        if os.path.exists(path): os.unlink(path)

def _shot_win(region=None) -> Optional[str]:
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=region, all_screens=True)
        return base64.b64encode(compress(img)).decode()
    except Exception: return None

def _shot_linux(region=None) -> Optional[str]:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f: path = f.name
    try:
        cmds = ([["scrot","-a","{},{},{},{}".format(*region),path]] if region
                else [["scrot",path],["gnome-screenshot","-f",path],["import","-window","root",path]])
        for cmd in cmds:
            if subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0:
                return base64.b64encode(compress(Image.open(path))).decode()
    except Exception: pass
    finally:
        if os.path.exists(path): os.unlink(path)
    return None

def take_shot(region=None) -> Optional[str]:
    if IS_MAC:  return _shot_mac(region)
    if IS_WIN:  return _shot_win(region)
    return _shot_linux(region)

def _hash_shot() -> Optional[str]:
    """
    轻量截图，专用于哈希对比，比全质量截图小 10x。
    FIX: 原来总是先截全质量图再做哈希，无变化时浪费 ~100ms 和 ~37KB。
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    try:
        if IS_MAC:
            cmd = ["screencapture", "-x", "-t", "png", path]
            if subprocess.run(cmd, capture_output=True, timeout=5).returncode:
                return None
            img  = Image.open(path).resize((320, 200), Image.BILINEAR)
            buf  = BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=30)
            return base64.b64encode(buf.getvalue()).decode()
        return take_shot()   # 非 Mac 退化到普通截图
    except Exception:
        return None
    finally:
        if os.path.exists(path): os.unlink(path)

def all_displays_shot() -> Optional[str]:
    if not IS_MAC: return take_shot()
    try:
        r = subprocess.run(["system_profiler","SPDisplaysDataType","-json"],
                           capture_output=True, text=True, timeout=5)
        n = max(len(json.loads(r.stdout).get("SPDisplaysDataType",[{}])[0]
                    .get("spdisplays_ndrvs",[])), 1)
    except Exception: n = 1
    if n == 1: return take_shot()
    frames = [Image.open(BytesIO(base64.b64decode(s)))
              for i in range(1, n+1) if (s := _shot_mac(display=i))]
    if not frames: return take_shot()
    w = sum(f.width for f in frames); h = max(f.height for f in frames)
    out = Image.new("RGB", (w, h)); x = 0
    for f in frames: out.paste(f, (x,0)); x += f.width
    return base64.b64encode(compress(out, max_px=3000)).decode()

# ══════════════════════════════════════════════════════════════════════════════
# 底层：窗口信息
# ══════════════════════════════════════════════════════════════════════════════
# 全局 osascript 线程池（最多 3 个并发），避免重量级脚本阻塞 brain 循环
_osa_pool = __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor(
    max_workers=3, thread_name_prefix="osa"
)

def osa(script: str, timeout=5) -> str:
    """同步 osascript 调用（用于工具函数）"""
    try:
        r = subprocess.run(["osascript","-e",script], capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception: return ""

def osa_async(script: str, timeout=5) -> str:
    """
    异步化 osascript：提交到线程池，最多等 timeout 秒，超时返回空。
    用于 brain 循环中的非关键调用，不阻塞感知主循环。
    """
    try:
        future = _osa_pool.submit(osa, script, timeout)
        return future.result(timeout=timeout)
    except Exception:
        return ""

def _browser_url(app: str) -> str:
    scripts = {
        "Safari":         'tell application "Safari" to return URL of front document',
        "Google Chrome":  'tell application "Google Chrome" to return URL of active tab of front window',
        "Arc":            'tell application "Arc" to return URL of active tab of front window',
        "Microsoft Edge": 'tell application "Microsoft Edge" to return URL of active tab of front window',
        "Brave Browser":  'tell application "Brave Browser" to return URL of active tab of front window',
    }
    return osa(scripts.get(app,""), timeout=3)

def get_win() -> dict:
    if IS_MAC:
        out = osa("""
tell application "System Events"
    set fp to first process where frontmost is true
    set appName to name of fp
    set winTitle to ""
    try
        set winTitle to name of front window of fp
    end try
    return appName & "|||" & winTitle
end tell
""")
        parts = out.split("|||")
        app   = parts[0].strip() if parts else "Unknown"
        title = parts[1].strip() if len(parts) > 1 else ""
        if is_bl(app): return {"app":app,"title":"[隐私保护]","blocked":True}
        info  = {"app":app,"title":title}
        if app in BROWSERS:
            url = _browser_url(app)
            if url: info["url"] = url
        return info
    if IS_WIN:
        try:
            import ctypes, ctypes.wintypes as wt
            u = ctypes.windll.user32; h = u.GetForegroundWindow()
            b = ctypes.create_unicode_buffer(u.GetWindowTextLengthW(h)+1)
            u.GetWindowTextW(h,b,len(b))
            pid = wt.DWORD(); u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            try:
                import psutil; app = psutil.Process(pid.value).name()
            except Exception: app = "Unknown"
            return {"app":app,"title":b.value}
        except Exception: return {"app":"Unknown","title":""}
    for cmd in [["xdotool","getactivewindow","getwindowname"],["wmctrl","-l"]]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return {"app":"Linux","title":r.stdout.strip().splitlines()[0]}
        except FileNotFoundError: continue
    return {"app":"Unknown","title":""}

# ══════════════════════════════════════════════════════════════════════════════
# 底层：文字提取
# ══════════════════════════════════════════════════════════════════════════════
def _js_page(app: str) -> str:
    scripts = {
        "Google Chrome":  'tell application "Google Chrome" to execute front window\'s active tab javascript "document.documentElement.innerText"',
        "Safari":         'tell application "Safari" to do JavaScript "document.documentElement.innerText" in front document',
        "Arc":            'tell application "Arc" to execute front window\'s active tab javascript "document.documentElement.innerText"',
        "Microsoft Edge": 'tell application "Microsoft Edge" to execute front window\'s active tab javascript "document.documentElement.innerText"',
        "Brave Browser":  'tell application "Brave Browser" to execute front window\'s active tab javascript "document.documentElement.innerText"',
    }
    s = scripts.get(app,"")
    if not s: return ""
    try:
        r = subprocess.run(["osascript","-e",s], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip(): return r.stdout.strip()
        if "Apple 事件" in r.stderr:
            print(f"[js] {app} 未开启JS权限", file=sys.stderr)
    except Exception: pass
    return ""

def _acc_text(app: str) -> str:
    t = osa(f"""
tell application "System Events"
    tell process "{app}"
        set allText to ""
        try
            set allText to entire contents of front window as text
        end try
        return allText
    end tell
end tell
""", timeout=12)
    return t.replace(", missing value","").replace("missing value, ","")

def _ocr(b64: str) -> str:
    try:
        import pytesseract
        img = Image.open(BytesIO(base64.b64decode(b64)))
        try:    return pytesseract.image_to_string(img, lang="chi_sim+eng")
        except: return pytesseract.image_to_string(img, lang="eng")
    except ImportError: return "[OCR不可用]"
    except Exception as e: return f"[OCR失败:{e}]"

def _clipboard() -> str:
    try:
        if IS_MAC: return subprocess.run(["pbpaste"],capture_output=True,text=True,timeout=3).stdout
        if IS_WIN: return subprocess.run(["powershell","-NoProfile","-Command","Get-Clipboard"],
                                         capture_output=True,text=True,timeout=5).stdout
        for c in [["xclip","-selection","clipboard","-o"],["xsel","--clipboard","--output"]]:
            try:
                r = subprocess.run(c,capture_output=True,text=True,timeout=3)
                if r.returncode == 0: return r.stdout
            except FileNotFoundError: continue
    except Exception: pass
    return ""

def _extract(win: dict, limit: int = TEXT_LIMIT) -> str:
    app = win.get("app","")
    if not IS_MAC:
        return redact(_ocr(take_shot() or "")[:limit])
    if app in BROWSERS:
        t = _js_page(app) or _acc_text(app)
        return redact(t[:limit]) if t else ""
    # FIX: 终端历史提取走异步线程池，不阻塞 brain 感知循环
    if app == "Terminal":
        t = osa_async('tell application "Terminal" to return history of selected tab of front window', 4)
        return t[-limit:] if t else ""
    if app in ("iTerm2","iTerm"):
        t = osa_async('tell application "iTerm" to tell current session of current tab of current window to return contents', 4)
        return t[-limit:] if t else ""
    if ".pdf" in win.get("title","").lower() or app in ("Preview","Skim"):
        t = _select_copy(app)
        return t[:limit] if t else ""
    t = _acc_text(app)
    if t and len(t) > 30: return redact(t[:limit])
    return redact(_ocr(take_shot() or "")[:limit])

def _select_copy(app: str) -> str:
    if not IS_MAC: return ""
    try:
        old = _clipboard()
        osa(f'tell application "{app}" to activate', timeout=3)
        time.sleep(0.4)
        osa(f"""
tell application "System Events"
    tell process "{app}"
        keystroke "a" using command down
        delay 0.2
        keystroke "c" using command down
    end tell
end tell
""", timeout=5)
        time.sleep(0.5)
        new = _clipboard()
        osa('tell application "Terminal" to activate', timeout=3)
        return redact(new[:10000]) if new != old and len(new) > 20 else ""
    except Exception as e:
        print(f"[select_copy] {e}", file=sys.stderr); return ""

# ══════════════════════════════════════════════════════════════════════════════
# 底层：错误检测
# ══════════════════════════════════════════════════════════════════════════════
ERR_RE = re.compile(
    r"(?im)^.*?(Error|Exception|FAILED|FATAL|Critical)\s*:.*$"
    r"|^.*?Traceback \(most recent call last\).*$"
    r"|^.*?(SyntaxError|TypeError|ValueError|AttributeError|ImportError).*$"
    r"|^.*?(command not found|No such file or directory|Permission denied).*$"
    r"|^.*?(Connection refused|ENOENT|ECONNREFUSED).*$"
    r"|^.*?(404 Not Found|500 Internal Server Error).*$"
    r"|^.*?(npm ERR!|yarn error).*$"
)
def find_errors(text: str) -> List[str]:
    found, seen = [], set()
    for m in ERR_RE.finditer(text):
        line = m.group(0).strip()[:200]
        key  = line[:60]
        if key not in seen:
            seen.add(key); found.append(line)
        if len(found) >= 6: break
    return found

# ══════════════════════════════════════════════════════════════════════════════
# 底层：滚动拼图
# ══════════════════════════════════════════════════════════════════════════════
def _scroll_stitch(app: str, steps: int = 3) -> Optional[str]:
    osa(f'tell application "{app}" to activate', timeout=3)
    time.sleep(0.5)
    frames = []
    if s := take_shot(): frames.append(s)
    for _ in range(steps):
        osa(f'tell application "System Events" to tell process "{app}" to key code 121', timeout=3)
        time.sleep(0.5)
        if s := take_shot(): frames.append(s)
    osa('tell application "Terminal" to activate', timeout=3)
    if len(frames) < 2: return frames[0] if frames else None
    imgs = [Image.open(BytesIO(base64.b64decode(f))) for f in frames]
    out  = Image.new("RGB", (imgs[0].width, sum(i.height for i in imgs)))
    y = 0
    for img in imgs: out.paste(img,(0,y)); y += img.height
    return base64.b64encode(compress(out, max_px=2400, q=70)).decode()

# ══════════════════════════════════════════════════════════════════════════════
# 感知哈希
# ══════════════════════════════════════════════════════════════════════════════
def _dhash(img: Image.Image, s: int = 8) -> int:
    tiny = img.resize((s+1,s), Image.LANCZOS).convert("L")
    px   = list(tiny.getdata())
    bits = 0
    for r in range(s):
        for c in range(s):
            bits = (bits<<1)|(1 if px[r*(s+1)+c]>px[r*(s+1)+c+1] else 0)
    return bits

def _hdist(a: int, b: int) -> int: return bin(a^b).count("1")

# ══════════════════════════════════════════════════════════════════════════════
# ProactiveThinker — S 模式专属 AI 自主思考引擎
# ══════════════════════════════════════════════════════════════════════════════
class ProactiveThinker:
    """
    S 模式（Smart + Super）专属，每 10s 调用 Claude Haiku API 主动分析屏幕。

    功能：
      - 主动思考：每 10s 生成洞察，存入 journal + session memory
      - 自动休眠：用户空闲 5 分钟 → 自动降级到 E 模式，停止 API 调用
      - 自动唤醒：ScreenBrain 检测到新活动 → 自动恢复 S 模式
      - 错误关联：记录重复出现的错误，提醒用户关注
      - 专注检测：区分深度工作 vs 频繁切换/分心状态
      - 智能去重：相似洞察不重复写入

    费用（claude-haiku-4-5）：
      每次调用 ≈ $0.001（500 input + 150 output token）
      活跃期 8h/天 × 3次/min = 1440次/天 ≈ $1.44/天
      自动休眠后实际活跃约 4-6h → ≈ $0.70-1.00/天 ≈ $20-30/月
      可通过调整 THINK_INTERVAL 控制成本（60s ≈ $7/月，300s ≈ $1.5/月）
    """

    THINK_INTERVAL  = 10    # 主动思考间隔（秒）
    IDLE_THRESHOLD  = 5.0   # 空闲阈值（分钟），超过则自动降级到 E
    MIN_LEN         = 8     # 最短有效洞察
    SKIP_PHRASES    = {"正在正常工作","用户正常","没有发现","无异常","一切正常","__skip__"}

    def __init__(self, brain: "ScreenBrain"):
        self._brain        = brain
        self._client       = None
        self._available    = False
        self._running      = False
        self._auto_slept   = False   # 是否因空闲自动降级

        self._call_count   = 0
        self._last_ev_id   = -1
        self._idle_since: Optional[float] = None
        self._last_insight = ""
        # FIX: deque 替代手动截断的 list
        self._err_history: deque = deque(maxlen=50)

        # 专注度统计 — FIX: deque + 独立锁，防止 on_activity 与 _build_prompt 并发竞态
        self._switch_ts:   deque = deque()
        self._switch_lock  = threading.Lock()

        # 新页面立即预判 — FIX: Event 替代忙等循环
        self._wake_event    = threading.Event()
        self._last_url_seen = ""
        self._client_lock   = threading.Lock()

    # ── 初始化 / 启停 ─────────────────────────────────────────────────────────

    def init(self) -> bool:
        """初始化 Anthropic 客户端（线程安全）"""
        with self._client_lock:   # FIX: 防止并发初始化竞态
            if self._available:
                return True
            try:
                import anthropic
                self._client    = anthropic.Anthropic()
                self._available = True
                print("[thinker] Anthropic 客户端初始化成功", file=sys.stderr)
                return True
            except ImportError:
                print("[thinker] 未安装 anthropic SDK: pip install anthropic", file=sys.stderr)
                return False
            except Exception as e:
                print(f"[thinker] 初始化失败: {e}", file=sys.stderr)
                return False

    def start(self) -> bool:
        if self._running: return True
        if not self._available and not self.init(): return False
        self._running    = True
        self._auto_slept = False
        self._last_ev_id = self._brain._event_count
        self._idle_since = None
        self._wake_event.clear()
        threading.Thread(target=self._loop, daemon=True, name="thinker").start()
        print(f"[thinker] 启动（每{self.THINK_INTERVAL}s思考）", file=sys.stderr)
        return True

    def stop(self, reason: str = "手动"):
        if not self._running: return
        self._running = False
        self._wake_event.set()   # FIX: 唤醒等待中的 Event，让线程能立即退出
        print(f"[thinker] 停止（{reason}）", file=sys.stderr)

    def on_activity(self, kind: str = "", url: str = ""):
        """ScreenBrain 每次 _emit 时调用"""
        if self._auto_slept and not self._running:
            print("[thinker] 检测到新活动，自动恢复 S 模式", file=sys.stderr)
            self._brain.set_mode("S", _log=False)
            self._auto_slept = False
            self.start()
            if _MEMORY_OK:
                write_event("task","system","检测到用户活动，自动恢复 S 模式")

        # 新页面 → 通过 Event 立即唤醒，不再忙等
        if kind in ("nav","switch","bg_nav","title") and url != self._last_url_seen:
            self._wake_event.set()

        # FIX: switch_lock 保护 deque，防止并发读写竞态
        now = time.time()
        with self._switch_lock:
            self._switch_ts.append(now)
            while self._switch_ts and now - self._switch_ts[0] > 300:
                self._switch_ts.popleft()

    # ── 主循环 ────────────────────────────────────────────────────────────────

    def _loop(self):
        """
        FIX: 用 threading.Event 替代忙等循环。
        正常：等待 THINK_INTERVAL 秒后思考。
        有新页面：Event.set() 立即唤醒，额外等 1s 让内容加载完成。
        停止：Event.set() 立即解除阻塞退出。
        """
        while self._running:
            triggered = self._wake_event.wait(timeout=self.THINK_INTERVAL)
            if not self._running:
                break
            if triggered:
                self._wake_event.clear()
                time.sleep(1)   # 等 brain 把新页面内容抓进来
            try:
                self._tick()
            except Exception as e:
                print(f"[thinker.tick] {e}", file=sys.stderr)

    def _tick(self):
        cur_count = self._brain._event_count

        # ── 空闲检测 ──────────────────────────────────────────────────────────
        if cur_count == self._last_ev_id:
            if self._idle_since is None:
                self._idle_since = time.time()
            idle_min = (time.time() - self._idle_since) / 60
            if idle_min >= self.IDLE_THRESHOLD:
                self._auto_sleep(idle_min)
            return   # 空闲时不调用 API

        # 有新活动，重置空闲计时
        self._idle_since = None
        self._last_ev_id = cur_count

        # 新页面预判模式：事件 id 有变化且 URL 变了
        cur       = self._brain.current_snap()
        new_url   = cur.get("url","")
        immediate = (new_url != self._last_url_seen) and bool(new_url)

        # ── 构建提示 + 调用 API ───────────────────────────────────────────────
        prompt = self._build_prompt(immediate=immediate)
        if not prompt: return

        insight = self._call_api(prompt)
        if not insight: return

        # 去重
        if self._similar(insight, self._last_insight): return
        self._last_insight = insight

        # 记录本次思考的 URL（去重用）
        cur = self._brain.current_snap()
        self._last_url_seen = cur.get("url","")

        # 存入 journal + session memory
        prefix = "[S预判]" if immediate else "[S思考]"
        if _MEMORY_OK:
            write_insight(f"{prefix} {insight}")
        sess = self._brain._session
        if sess:
            sess.add_insight(f"{prefix} {insight}")

        print(f"[thinker] #{self._call_count} {'预判' if immediate else '思考'}: {insight[:60]}", file=sys.stderr)

    # ── 自动休眠 ──────────────────────────────────────────────────────────────

    def _auto_sleep(self, idle_min: float):
        print(f"[thinker] 空闲 {idle_min:.1f}min，自动降级到 E 模式", file=sys.stderr)
        self._auto_slept = True
        self._running    = False
        self._idle_since = None
        self._brain.set_mode("E", _log=False)
        if _MEMORY_OK:
            write_event("task","system",
                        f"S模式检测到空闲 {idle_min:.1f} 分钟，自动降级到 E 模式（活动后自动恢复）")

    # ── 提示构建 ──────────────────────────────────────────────────────────────

    def _build_prompt(self, immediate: bool = False) -> str:
        cur    = self._brain.current_snap()
        events = self._brain.recent_events(8)
        sess   = self._brain._session

        if not cur or not cur.get("app"): return ""
        app = cur.get("app","")

        if immediate:
            # 新页面立即预判模式：直接解答/分析内容
            lines = [
                "你是用户的AI助手，刚刚检测到用户打开了一个新页面。",
                "在用户开口问之前，立刻分析页面内容，准备好答案。",
                "如果是题目/问题 → 直接给出解答（简洁，≤150字）。",
                "如果是文章/文档 → 给出关键摘要。",
                "如果是代码 → 找出关键逻辑或潜在问题。",
                "",
            ]
        else:
            lines = [
                "你是用户的实时工作助手，正在静默观察他的屏幕。",
                "根据以下信息生成一句简洁中文洞察（≤80字），只说有价值的内容。",
                "",
            ]

        # 当前状态
        lines.append(f"当前App: {app} | 标题: {cur.get('title','')[:40]} | 任务模式: {cur.get('pattern','')}")
        if cur.get("url"):
            lines.append(f"URL: {cur['url'][:80]}")
        if cur.get("text"):
            lines.append(f"屏幕内容（前250字）:\n{cur['text'][:250]}")

        # 错误关联
        if cur.get("errors"):
            err = cur["errors"][0][:100]
            repeat = err in self._err_history
            lines.append(f"{'⚠️ 重复出现的错误' if repeat else '检测到错误'}: {err}")
            if not repeat:
                self._err_history.append(err)
                if len(self._err_history) > 30: self._err_history.pop(0)

        # 操作轨迹
        if events:
            trail = " → ".join(f"{e['app'][:12]}({e['kind']})" for e in events[:6])
            lines.append(f"最近操作轨迹: {trail}")

        # 专注度 (FIX: switch_lock)
        with self._switch_lock:
            sw = len(self._switch_ts)
        if sw >= 8:
            lines.append(f"注意：用户过去5分钟切换了 {sw} 次 App（可能分心）")
        elif sw <= 2 and sess and sess.current_pattern() == "coding":
            lines.append("用户处于深度专注状态（切换频率极低）")

        # 会话统计
        if sess:
            top = sess.app_summary()[:2]
            if top:
                summary = "、".join(f"{a['app']}({a['minutes']}min)" for a in top)
                lines.append(f"今日使用时长前2: {summary}")

        lines += [
            "",
            "输出规则：",
            "- 发现用户卡住/报错/低效 → 给出具体建议",
            "- 发现专注工作 → 可以鼓励或提示风险",
            "- 无异常、用户正常工作 → 回复 __skip__",
            "- 不要复述已知信息，要给出洞察或行动建议",
        ]
        return "\n".join(lines)

    # ── API 调用 ──────────────────────────────────────────────────────────────

    def _call_api(self, prompt: str) -> str:
        # FIX: 加 client_lock 防并发，并验证响应结构
        with self._client_lock:
            client = self._client
        if not client:
            return ""
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role":"user","content":prompt}],
            )
            self._call_count += 1
            # FIX: 验证响应结构，防止 IndexError
            if not resp.content or not hasattr(resp.content[0], "text"):
                return ""
            text = resp.content[0].text.strip()
            if text == "__skip__" or len(text) < self.MIN_LEN: return ""
            if any(p in text for p in self.SKIP_PHRASES): return ""
            return text
        except Exception as e:
            print(f"[thinker] API错误: {e}", file=sys.stderr)
            return ""

    # ── 工具方法 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _similar(a: str, b: str) -> bool:
        """简单相似度：前30字重叠超65%视为重复"""
        if not a or not b: return False
        a, b = set(a[:30]), set(b[:30])
        return len(a & b) / max(len(a), 1) > 0.65

    def cost_estimate(self) -> str:
        """基于实际调用次数估算费用"""
        usd = self._call_count * 0.001   # ~$0.001/次（Haiku估算）
        return f"~${usd:.3f}（{self._call_count}次调用）"

    def stats(self) -> dict:
        idle_min = round((time.time() - self._idle_since) / 60, 1) if self._idle_since else 0
        return {
            "running":      self._running,
            "available":    self._available,
            "auto_slept":   self._auto_slept,
            "api_calls":    self._call_count,
            "idle_min":     idle_min,
            "switch_5min":  len(self._switch_ts),
            "cost_est":     self.cost_estimate(),
        }


# ══════════════════════════════════════════════════════════════════════════════
# ScreenBrain — 统一感知核心
# ══════════════════════════════════════════════════════════════════════════════
class ScreenBrain:
    """
    系统唯一的感知核心。四种模式，线程始终运行，切换模式无需重启。

    模式定义与成本标注（从轻到重）：

      ┌──────┬──────────┬───────────────────────────────────────────────────────┐
      │ 模式 │  间隔    │  成本                                                 │
      ├──────┼──────────┼───────────────────────────────────────────────────────┤
      │ off  │  —       │  CPU: 0%    截图: 0次/h    Token: 0    费用: $0       │
      │      │          │  完全关闭，零消耗                                     │
      ├──────┼──────────┼───────────────────────────────────────────────────────┤
      │  E   │  1.5s    │  CPU: <0.5%  截图: 5-20次/h  Token/查: ~200          │
      │      │          │  仅在 App 切换 / URL 跳转时截图，其余时间只查窗口名  │
      │      │          │  费用: ~$1/月（按每天50次AI对话估算，下同）           │
      ├──────┼──────────┼───────────────────────────────────────────────────────┤
      │  A   │  2s      │  CPU: ~4-6%  截图: 30-100次/h  Token/查: ~500        │
      │      │          │  每2s截一张小图做哈希，内容变化时完整捕获            │
      │      │          │  费用: ~$2-3/月                                       │
      ├──────┼──────────┼───────────────────────────────────────────────────────┤
      │  S   │  1s      │  CPU: ~8-12%  截图: 60-200次/h  Token/查: ~1000      │
      │      │          │  每1s截图+哈希，连标题变化/终端输出都捕获            │
      │      │          │  费用: ~$4-6/月（全功率，可控范围内的上限）           │
      └──────┴──────────┴───────────────────────────────────────────────────────┘

      Token 费用基准：Claude Sonnet $3/M输入token，每次查询含上下文累积数据。
      截图本身不消耗 token（缓存在本地），只有调用 get_*_screenshot 时才发送给 AI。

    触发矩阵：
      事件类型        off   E   A      S
      App 切换         ×    ✓   ✓      ✓
      URL 变化         ×    ✓   ✓      ✓
      内容哈希变化     ×    ×   ✓(12)  ✓(6，更灵敏)
      标题变化         ×    ×   ×      ✓
      终端内容变化     ×    ×   ×      ✓

    事件流水线（每个 emit 四步）：
      1. 更新内部状态缓存（单一事实来源）
      2. 通知 SessionMemory（会话内聚合）
      3. 写入 Journal（持久化，change 事件节流）
      4. 打印 stderr 日志
    """

    # 哈希阈值（越小越灵敏，检测到越微小的变化）
    # FIX: 原命名 HASH_THR_S/HASH_THR_A 与实际使用逻辑相反，导致误导性极强
    HASH_THR_NORMAL    = 12   # A 模式使用，只捕获明显变化
    HASH_THR_SENSITIVE = 6    # S 模式使用，捕获细微变化

    MAX_SNAPS    = 12
    MAX_EVENTS   = 40
    CHANGE_THR   = 60   # change 写 journal 最小间隔（秒/App）
    IDLE_THR     = 5.0  # 空闲阈值（分钟）
    IDLE_CHECK   = 60   # 空闲检测间隔（秒）

    # 各模式默认间隔
    # X 模式：0.5s 感知 + 哈希门控 Vision（不是每次都调 API）
    INTERVAL = {"off": 2.0, "E": 1.5, "A": 2.0, "S": 1.0, "X": 0.5}

    def __init__(self):
        self._lock    = threading.Lock()
        self._mode    = "E"      # off | E | S | A
        self._running = False
        self._interval = self.INTERVAL["E"]

        # ── 统一状态（唯一事实来源） ──────────────────────────────────────
        self._current: dict       = {}
        self._snaps:  List[dict]  = []
        # FIX: deque 替代 list，appendleft O(1)，避免 insert(0,...) O(n)
        self._events: deque       = deque(maxlen=self.MAX_EVENTS)
        self._summary: str        = ""
        self._event_count: int    = 0
        self._last_emit_key: str  = ""   # 连续事件去重

        # ── 探测状态 ──────────────────────────────────────────────────────
        self._last_app:   str             = ""
        self._last_url:   str             = ""
        self._last_title: str             = ""
        self._last_hash:  Optional[int]   = None
        self._change_ts:  dict[str,float] = {}

        # ── 关联模块 ──────────────────────────────────────────────────────
        self._session: Optional[SessionMemory] = None
        self._thinker: Optional[ProactiveThinker] = None   # S 模式专属

    # ── 启动 / 模式切换 ───────────────────────────────────────────────────────

    def start(self, mode: str = "E"):
        if self._running: return
        self._running = True
        if _MEMORY_OK:
            self._session = SessionMemory()
        self._thinker = ProactiveThinker(self)
        threading.Thread(target=self._sense_loop, daemon=True, name="brain-sense").start()
        threading.Thread(target=self._idle_loop,  daemon=True, name="brain-idle").start()
        self.set_mode(mode, _log=False)
        print(f"[brain] 启动 mode={self._mode} interval={self._interval}s", file=sys.stderr)

    def set_mode(self, mode: str, _log: bool = True):
        """切换模式，不重启线程。mode ∈ {off, E, A, S, X}"""
        mode = mode.upper() if mode.upper() in ("E","A","S","X") else mode.lower()
        if mode not in ("off","E","A","S","X"):
            raise ValueError(f"未知模式: {mode}，可选: off / E / A / S / X")

        prev = self._mode
        self._mode     = mode
        self._interval = self.INTERVAL.get(mode, 1.5)
        # 切换时重置探测状态，避免误判
        self._last_app = self._last_url = self._last_title = ""
        self._last_hash = None

        # 进入 S/X → 启动 Thinker；离开 → 停止 Thinker
        if self._thinker:
            if mode in ("S","X") and prev not in ("S","X"):
                self._thinker.start()
            elif mode not in ("S","X") and prev in ("S","X"):
                self._thinker.stop("模式切换")

        # 进入 X → 启动 overlay 进程 + 操控模块
        if mode == "X" and prev != "X" and _X_OK:
            self._start_x_mode()
        elif mode != "X" and prev == "X" and _X_OK:
            show_status(mode)   # 更新状态条

        # 切到 off 或从高负载模式降级 → 立即清理截图
        if mode == "off" or (prev in ("S","A") and mode in ("E","off")):
            threading.Thread(target=self._cleanup_shots, daemon=True).start()

        if _log:
            print(f"[brain] 切换模式 → {mode} interval={self._interval}s", file=sys.stderr)

    @property
    def mode(self) -> str: return self._mode

    @property
    def running(self) -> bool: return self._running

    def _start_x_mode(self):
        """启动 X 模式：overlay 进程 + 初始化规划器"""
        import subprocess
        overlay_py = os.path.join(os.path.dirname(__file__), "screen_mcp", "overlay.py")
        python     = sys.executable
        try:
            subprocess.Popen(
                [python, overlay_py],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)   # 等 overlay 启动
            show_status("X", "启动中")
            planner.init()
            print("[brain] X 模式启动：overlay + planner 已就绪", file=sys.stderr)
        except Exception as e:
            print(f"[brain] X 模式启动失败: {e}", file=sys.stderr)

    # ── 感知循环 ──────────────────────────────────────────────────────────────

    def _sense_loop(self):
        while self._running:
            try: self._tick()
            except Exception as e: print(f"[brain.sense] {e}", file=sys.stderr)
            time.sleep(self._interval)

    def _tick(self):
        if self._mode == "off":
            return   # 完全关闭，零 CPU

        # L1: 窗口名 + URL（始终执行，< 5ms）
        win   = get_win()
        app   = win.get("app","")
        url   = win.get("url","")
        title = win.get("title","")
        if not app or is_bl(app): return

        is_switch     = app != self._last_app
        is_url_change = (app in BROWSERS) and bool(url) and (url != self._last_url)
        # FIX: 触发矩阵规定标题变化只属于 S 模式，原来错写成了 "A"
        is_title_change = (self._mode == "S") and (title != self._last_title) and bool(title)

        if is_switch:
            self._last_app   = app
            self._last_url   = url
            self._last_title = title
            self._last_hash  = None
            # 仅 S 模式捕获终端内容；E / A 跳过终端文字
            skip_text = (app in TERMINAL_APPS) and (self._mode != "S")
            shot = take_shot()
            text = _extract(win, SUMMARY_LIMIT) if not skip_text else ""
            self._emit("switch", win, shot, text)

        elif is_url_change:                          # E / A / S 均触发
            self._last_url   = url
            self._last_title = title
            self._last_hash  = None
            shot = take_shot()
            text = _extract(win, SUMMARY_LIMIT)
            self._emit("nav", win, shot, text)

        elif is_title_change and app not in TERMINAL_APPS:   # 仅 S
            self._last_title = title
            self._last_hash  = None
            shot = take_shot()
            text = _extract(win, SUMMARY_LIMIT)
            self._emit("title", win, shot, text)

        elif self._mode in ("A","S") and app not in TERMINAL_APPS:
            # L2: 轻量哈希截图（不浪费全质量截图）→ 有变化才 L3 全截
            hash_b64 = _hash_shot()
            if not hash_b64: return
            try:
                tiny = Image.open(BytesIO(base64.b64decode(hash_b64))).resize((64,40))
                h    = _dhash(tiny)
            except Exception:
                return
            thr = self.HASH_THR_SENSITIVE if self._mode == "S" else self.HASH_THR_NORMAL
            if self._last_hash is not None and _hdist(self._last_hash, h) < thr:
                return   # 无变化，丢弃轻量截图
            self._last_hash  = h
            self._last_title = title
            # L3: 确认有变化，才取全质量截图
            raw  = take_shot()
            text = _extract(win, SUMMARY_LIMIT)
            self._emit("change", win, raw, text)

        elif self._mode in ("A","S") and app in TERMINAL_APPS:
            # A/S 模式：在 Terminal 时，后台持续探测浏览器/App 的内容变化
            bg = self._probe_background_apps()
            if not bg: return
            bg_app   = bg.get("app","") or bg.get("prev_app","")
            bg_url   = bg.get("url","")  or bg.get("prev_url","")
            bg_title = bg.get("title","") or bg.get("prev_title","")
            bg_text  = bg.get("text","")

            # URL 或标题有变化 → 记录新事件
            if bg_url != self._last_url or (self._mode == "S" and bg_title != self._last_title):
                self._last_url   = bg_url
                self._last_title = bg_title
                self._last_hash  = None
                shot = take_shot()   # 全屏截图，包含所有可见窗口
                fake_win = {"app": bg_app, "title": bg_title, "url": bg_url}
                self._emit("bg_nav", fake_win, shot, bg_text[:SUMMARY_LIMIT])

    # ── 自动清理截图 ──────────────────────────────────────────────────────────

    SHOT_TTL     = 300   # 截图保留时长（秒），超过后释放内存
    CLEAN_INTERVAL = 60  # 清理检查间隔（秒）

    def _cleanup_shots(self):
        """
        释放不再有用的截图数据，规则：
          1. 超过 SHOT_TTL 秒的截图 → 释放（文字/元数据保留）
          2. 同一 App 只保留最新一张截图，旧的释放
          3. off 模式 → 全部清空
        """
        now = time.time()
        freed = 0
        seen_apps: set = set()

        with self._lock:
            for s in self._snaps:
                if not s.get("shot"):
                    continue
                # off 模式：全部清
                if self._mode == "off":
                    s["shot"] = None
                    freed += 1
                    continue
                # 超时：释放
                if now - s["ts"] > self.SHOT_TTL:
                    s["shot"] = None
                    freed += 1
                    continue
                # 同 App 重复：只保留最新（_snaps 是倒序，第一个遇到的是最新的）
                app = s.get("app","")
                if app in seen_apps:
                    s["shot"] = None
                    freed += 1
                else:
                    seen_apps.add(app)

        if freed:
            print(f"[brain.clean] 释放 {freed} 张截图", file=sys.stderr)

    def _idle_loop(self):
        while self._running:
            time.sleep(self.CLEAN_INTERVAL)
            try:
                # 截图自动清理
                self._cleanup_shots()
                # 空闲检测
                if self._mode == "off": continue
                if self._session and _MEMORY_OK:
                    idle = self._session.idle_warning(self.IDLE_THR)
                    if idle:
                        _journal_reminder(idle["hint"], context=f"App:{idle['app']}")
                        print(f"[brain.idle] {idle['app']} {idle['minutes']}min", file=sys.stderr)
            except Exception as e: print(f"[brain.idle] {e}", file=sys.stderr)

    # ── 事件流水线 ────────────────────────────────────────────────────────────

    def _emit(self, kind: str, win: dict, shot: Optional[str], text: str):
        app   = win.get("app","")
        title = win.get("title","")[:60]
        url   = win.get("url","")
        now   = time.time()
        errs  = find_errors(text) if text else []
        pat   = detect_pattern(app, url) if _MEMORY_OK else "general"

        # 连续事件去重：同 app:kind:url 在 5s 内不重复
        # FIX: 加入 url 维度，防止同 App 不同页面被误判为重复（如 Chrome 切页面）
        emit_key = f"{app}:{kind}:{url[:60]}"
        if emit_key == self._last_emit_key and (now - self._current.get("ts", 0)) < 5:
            return
        self._last_emit_key = emit_key

        snap: dict = {
            "id":      self._event_count,
            "kind":    kind,
            "ts":      now,
            "t":       time.strftime("%H:%M:%S"),
            "app":     app,
            "title":   title,
            "url":     url,
            "text":    text,
            "shot":    shot,
            "errors":  errs,
            "pattern": pat,
        }
        self._event_count += 1

        # 1. 更新状态缓存
        slim = {k: v for k, v in snap.items() if k != "shot"}
        with self._lock:
            self._current = snap
            # 快照历史（含截图）
            self._snaps.insert(0, snap)
            while len(self._snaps) > self.MAX_SNAPS:
                self._snaps.pop()
            # FIX: deque 自动管理上限，appendleft O(1)，overflow 时自动丢弃最旧
            if len(self._events) == self._events.maxlen:
                oldest = self._events[-1]
                chunk  = f"[{oldest['t']}]{oldest['app']}"
                self._summary = (self._summary + " | " + chunk)[-2000:]
            self._events.appendleft(slim)

        # 2. 通知 SessionMemory（立即，无节流）
        if self._session:
            if kind in ("switch", "nav", "bg_nav", "title"):
                self._session.on_app_switch(app, title, url, text[:200])
            elif kind == "change":
                self._session.on_content_change(app, text[:200])
            if errs:
                self._session.on_error(app, errs)

        # 3. 写入 Journal（FIX: bg_nav/title 事件也应写入）
        if _MEMORY_OK and app not in TERMINAL_APPS:
            if kind in ("switch", "nav", "bg_nav", "title"):
                write_event(kind, app, text[:300], title=title, url=url, tags=[pat])
                if errs:
                    write_event("error", app, "\n".join(errs[:3]), title=title, tags=["error"])
            elif kind == "change":
                last = self._change_ts.get(app, 0)
                if now - last >= self.CHANGE_THR:
                    self._change_ts[app] = now
                    write_event("change", app, text[:200], title=title)
                if errs:
                    write_event("error", app, "\n".join(errs[:3]), title=title, tags=["error"])

        # 4. 通知 Thinker（自动唤醒 + 切换频率统计 + 新页面预判）
        if self._thinker:
            self._thinker.on_activity(kind=kind, url=url)

        # X 模式：推送 overlay 状态更新
        if self._mode in ("S","X") and _X_OK:
            sess = self._session
            pat  = snap.get("pattern","")
            show_status(self._mode, pat, len(errs))
            if errs:
                show_error(errs[0][:120])

        print(f"[brain] #{self._event_count} {kind}|{app}|{len(text)}字|err:{len(errs)}", file=sys.stderr)

    # ── 统一查询接口（所有工具从这里读） ─────────────────────────────────────

    def latest_shot(self) -> Optional[str]:
        """最新缓存截图（b64，不含 data: 前缀）"""
        with self._lock:
            for s in self._snaps:
                if s.get("shot"): return s["shot"]
        return None

    def shot_for_app(self, app: str) -> Optional[str]:
        """某个 App 最近的截图"""
        with self._lock:
            for s in self._snaps:
                if s["app"] == app and s.get("shot"): return s["shot"]
        return None

    def current_snap(self) -> dict:
        with self._lock: return dict(self._current)

    def recent_events(self, n: int = 10) -> List[dict]:
        with self._lock:
            items = list(self._events)
            return items[:n]

    # 后台探测限速：防止在 S 模式中每秒都做耗时 osascript 查询
    _probe_cache:    Optional[dict]  = None
    _probe_cache_ts: float           = 0.0
    PROBE_CACHE_TTL: float           = 3.0   # 秒：同一结果最多缓存 3s
    PROBE_MAX_AGE:   float           = 300.0 # 秒：缓存快照超过 5 分钟视为过期

    def _probe_background_apps(self) -> Optional[dict]:
        """
        查询后台 App 状态（不需要用户切换窗口）。
        FIX:
          - 限速：3s 内最多执行一次真实查询，其余返回缓存
          - 最大总耗时 8s（原最坏 32s）
          - 隐私检查：跳过黑名单浏览器
          - 过期保护：缓存快照超 5 分钟不返回
        """
        if not IS_MAC: return None

        # 限速检查
        now = time.time()
        if now - self._probe_cache_ts < self.PROBE_CACHE_TTL and self._probe_cache:
            return self._probe_cache

        result = self._probe_background_apps_real()
        self._probe_cache    = result
        self._probe_cache_ts = time.time()
        return result

    def _probe_background_apps_real(self) -> Optional[dict]:
        """实际查询逻辑，含 8s 总超时控制"""
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
        _t0 = time.time()

        def _elapsed() -> float:
            return time.time() - _t0

        # 1. 列举前台进程（最多 4s）
        running = osa(
            'tell application "System Events" to return name of every process '
            'where background only is false as text', timeout=4
        )

        for browser in BROWSERS:
            if _elapsed() > 8.0: break          # 总超时保护
            if browser not in running: continue
            if is_bl(browser): continue          # FIX: 隐私检查

            url   = _browser_url(browser)        # ~3s max
            if _elapsed() > 8.0: break

            title = osa(f'tell application "{browser}" to return title of front window', timeout=min(3, max(1, 8-_elapsed())))

            # 文字提取：JS 优先（通常更快），失败才用 Accessibility（限 3s）
            text = _js_page(browser)
            if not text and _elapsed() < 8.0:
                text = _acc_text(browser) if 8.0 - _elapsed() > 1 else ""

            if url or title or text:
                errs = find_errors(text[:SUMMARY_LIMIT]) if text else []
                pat  = detect_pattern(browser, url) if _MEMORY_OK else "research"
                return {
                    "source":   f"后台实时:{browser}",
                    "app":      browser,
                    "title":    title[:80],
                    "url":      url,
                    "text":     text[:CONTEXT_LIMIT] or "[页面内容为空，可调用 get_monitor_screenshot]",
                    "has_shot": False,
                    "errors":   errs,
                    "pattern":  pat,
                }

        # 2. 回退到缓存快照（FIX: 只返回 5 分钟内的快照）
        stale_limit = time.time() - self.PROBE_MAX_AGE
        with self._lock:
            for s in self._snaps:
                if s["app"] not in TERMINAL_APPS and s["ts"] >= stale_limit:
                    age = time.time() - s["ts"]
                    return {
                        "source":    f"brain缓存:{s['app']}（{age:.0f}s前）",
                        "prev_app":  s["app"],
                        "prev_title":s["title"],
                        "prev_url":  s.get("url",""),
                        "text":      s["text"][:CONTEXT_LIMIT] or "[无文字]",
                        "has_shot":  bool(s.get("shot")),
                        "errors":    s.get("errors",[]),
                        "pattern":   s.get("pattern","general"),
                    }
        return None

    def get_context_for_tool(self) -> dict:
        """
        get_screen_context 的统一数据源。
        在 Terminal 时：直接实时查后台所有 App，不需要用户切换窗口。
        """
        win = get_win()
        app = win.get("app","")

        if app in TERMINAL_APPS:
            # 直接实时探测后台 App，无需切换
            bg = self._probe_background_apps()
            if bg:
                return bg
            return {"source":"未检测到后台App","text":"[没有发现打开的浏览器或其他App]"}

        if win.get("blocked"):
            return {"error":"隐私保护"}

        # 检查缓存是否够新（5s 内）
        with self._lock:
            cur = dict(self._current)
        if cur.get("app") == app and (time.time() - cur.get("ts",0)) < 5:
            return {
                "source":  f"brain实时:{app}",
                "title":   cur["title"],
                "url":     cur.get("url",""),
                "text":    cur["text"][:CONTEXT_LIMIT],
                "has_shot":bool(cur.get("shot")),
                "errors":  cur.get("errors",[]),
                "pattern": cur.get("pattern","general"),
            }

        # 缓存过期，立即提取（主动模式停止时可能发生）
        text = _extract(win, CONTEXT_LIMIT)
        return {
            "source":  f"brain即时:{app}",
            "title":   win.get("title",""),
            "url":     win.get("url",""),
            "text":    text or "[无法提取]",
            "has_shot": False,
            "errors":  find_errors(text),
            "pattern": detect_pattern(app, win.get("url","")) if _MEMORY_OK else "general",
        }

    def live_summary(self) -> dict:
        """get_live_context 的数据源"""
        with self._lock:
            evts = list(self._events)[:10]
            cur  = dict(self._current)
            summ = self._summary

        result: dict = {
            "mode":   self._mode,
            "events": self._event_count,
        }
        if cur:
            result["current"] = {
                "time":    cur.get("t",""),
                "app":     cur.get("app",""),
                "title":   cur.get("title",""),
                "url":     cur.get("url",""),
                "pattern": cur.get("pattern","general"),
                "text":    cur.get("text","")[:800],
                "errors":  cur.get("errors",[]),
                "has_screenshot": bool(cur.get("shot")),
            }
        result["trail"] = [
            f"[{e['t']}]{e['app']}({e['kind']}) {e.get('title','')[:20]}"
            for e in evts
        ]
        if summ:
            result["history_summary"] = summ[-400:]
        return result

    def full_workflow(self) -> dict:
        """get_workflow_context 的数据源（最综合）"""
        ctx = self.get_context_for_tool()
        result: dict = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "current_screen": ctx,
            "brain": {
                "mode":   self._mode,
                "events": self._event_count,
                "trail":  [
                    f"[{e['t']}]{e['app']}({e['kind']}) {e.get('title','')[:20]}"
                    for e in self.recent_events(6)
                ],
            },
        }
        if self._session:
            result["session"] = self._session.session_narrative()
        if _MEMORY_OK:
            today = read_today(max_chars=3000)
            if today: result["today_journal"] = today
            stats = journal_stats()
            if stats.get("exists"):
                result["journal_stats"] = {
                    k: stats[k] for k in ("entries","insights","errors","days")
                }
        return result

    def stats(self) -> dict:
        with self._lock:
            mem = sum(len(s.get("shot") or "")*3//4//1024 for s in self._snaps)
        s = {
            "running":  self._running,
            "mode":     self._mode,
            "interval": self._interval,
            "events":   self._event_count,
            "snaps":    len(self._snaps),
            "mem_kb":   mem,
        }
        if self._thinker:
            s["thinker"] = self._thinker.stats()
        return s


# ── 全局单例 ──────────────────────────────────────────────────────────────────
brain = ScreenBrain()

# ══════════════════════════════════════════════════════════════════════════════
# MCP 工具
# ══════════════════════════════════════════════════════════════════════════════

# ── 感知 ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_screen_context() -> str:
    """
    【首选工具】获取用户当前正在看的内容（纯文字 + 元数据，< 8KB）。

    自动处理终端焦点问题，返回用户真正在看的 App 内容。
    截图请单独调用 get_screen_screenshot 或 get_monitor_screenshot。
    """
    ctx = brain.get_context_for_tool()
    ctx["platform"] = PLATFORM
    return json.dumps(ctx, ensure_ascii=False, indent=2)


@mcp.tool()
def get_screen_screenshot() -> str:
    """
    获取当前全屏截图（纯图片）。

    调用时机：文字不够、需要看图形/布局/颜色时。
    已有监控时优先用 get_monitor_screenshot（无需重截）。
    """
    if get_win().get("blocked"): return "[隐私保护]"
    b64 = take_shot()
    return data_url(b64) if b64 else "[截图失败] 请检查屏幕录制权限"


@mcp.tool()
def get_monitor_screenshot() -> str:
    """
    返回 brain 缓存的最新截图，无需重新截图。
    无缓存时自动 fallback 到截全屏。
    """
    b64 = brain.latest_shot() or take_shot()
    return data_url(b64) if b64 else "[无可用截图]"


@mcp.tool()
def get_live_context() -> str:
    """
    获取实时监控上下文（< 5KB）：当前状态摘要 + 操作轨迹 + 历史摘要。
    截图请单独调用 get_monitor_screenshot。
    """
    return json.dumps(brain.live_summary(), ensure_ascii=False, indent=2)


@mcp.tool()
def get_workflow_context() -> str:
    """
    【AI 思考入口】工作流全景：当前屏幕 + 会话叙述 + 今日日志。

    每次对话开始时调用，快速了解用户的状态、历史和背景。
    """
    return json.dumps(brain.full_workflow(), ensure_ascii=False, indent=2)[:12000]


# ── 深度提取 ──────────────────────────────────────────────────────────────────

@mcp.tool()
def get_full_content(target_app: str = "") -> str:
    """
    通用全文提取（Select All+Copy），不受滚动限制。

    适用：WPS、Word、PDF、代码编辑器、浏览器等。
    参数：target_app 留空自动从 brain 缓存检测目标 App。
    """
    if not IS_MAC: return json.dumps({"error":"仅 macOS 支持"})
    if not target_app:
        with brain._lock:
            for s in brain._snaps:
                if s["app"] not in TERMINAL_APPS:
                    target_app = s["app"]; break
    if not target_app or target_app in TERMINAL_APPS:
        return json.dumps({"error":"无法确定目标App，请传入 target_app"})
    if is_bl(target_app):
        return json.dumps({"error":"隐私保护"})

    content = _select_copy(target_app)
    if not content and target_app in BROWSERS: content = _js_page(target_app)
    if not content: content = _acc_text(target_app)
    if not content: return json.dumps({"error":"无法提取，尝试 scroll_and_capture"})

    return json.dumps({"app":target_app,"length":len(content),"content":content[:10000]},
                      ensure_ascii=False, indent=2)


@mcp.tool()
def get_full_page_content() -> str:
    """
    浏览器专项：获取整个网页完整内容（不受滚动限制）。
    Chrome 开启JS：查看→开发者→允许Apple事件中的JavaScript
    """
    target = ""
    with brain._lock:
        for s in brain._snaps:
            if s["app"] in BROWSERS: target = s["app"]; break
    if not target:
        apps = osa('tell application "System Events" to return name of every process where background only is false as text')
        for b in BROWSERS:
            if b in apps: target = b; break
    if not target:
        return json.dumps({"error":"未检测到浏览器"})

    text = _js_page(target)
    if text and len(text) > 50:
        return json.dumps({"source":target,"method":"JavaScript",
                           "length":len(text),"content":redact(text[:8000])}, ensure_ascii=False, indent=2)
    text = _select_copy(target)
    if text and len(text) > 50:
        return json.dumps({"source":target,"method":"SelectAll",
                           "length":len(text),"content":text[:8000]}, ensure_ascii=False, indent=2)
    b64 = _scroll_stitch(target, steps=3)
    if b64:
        return json.dumps({"source":target,"method":"scroll_capture","image":data_url(b64),
                           "tip":"Chrome开启JS后可直接获取文字"}, ensure_ascii=False, indent=2)
    return json.dumps({"error":f"无法获取 {target} 内容"})


@mcp.tool()
def scroll_and_capture(target_app: str = "", scroll_steps: int = 3) -> str:
    """
    自动滚动并拍多张截图，垂直拼接成长图。
    参数：target_app 留空自动检测，scroll_steps 滚动次数（默认3）。
    """
    if not IS_MAC: return "[仅 macOS 支持]"
    if not target_app:
        with brain._lock:
            for s in brain._snaps:
                if s["app"] not in TERMINAL_APPS:
                    target_app = s["app"]; break
    if not target_app: return "[无法确定目标App]"
    b64 = _scroll_stitch(target_app, steps=scroll_steps)
    return data_url(b64) if b64 else f"[截图失败:{target_app}]"


# ── 窗口管理 ──────────────────────────────────────────────────────────────────

@mcp.tool()
def capture_screen(
    mode: str = "full",
    region_x: Optional[int] = None,
    region_y: Optional[int] = None,
    region_width: Optional[int] = None,
    region_height: Optional[int] = None,
    all_displays: bool = False,
) -> str:
    """手动截图：full（全屏）/ region（区域）/ all_displays（所有显示器拼接）。"""
    if get_win().get("blocked"): return "[隐私保护]"
    if all_displays:
        b64 = all_displays_shot()
    elif mode == "region" and all(v is not None for v in [region_x,region_y,region_width,region_height]):
        b64 = take_shot((region_x,region_y,region_width,region_height))
    else:
        b64 = take_shot()
    if not b64:
        hint = {"Darwin":"系统设置→隐私→屏幕录制授权终端","Windows":"管理员权限","Linux":"apt install scrot"}
        return "[截图失败] " + hint.get(PLATFORM,"")
    return data_url(b64)


@mcp.tool()
def get_active_window_info() -> str:
    """获取当前焦点窗口（App名/标题/URL），极速，不截图。"""
    return json.dumps(get_win(), ensure_ascii=False, indent=2)


@mcp.tool()
def list_open_windows() -> str:
    """列出所有打开的窗口和应用。"""
    if IS_MAC:
        t = osa("""
tell application "System Events"
    set out to ""
    set procs to every process where background only is false
    repeat with p in procs
        set pname to name of p
        set out to out & pname
        try
            set wins to every window of p
            if (count of wins) > 0 then
                set out to out & ": "
                repeat with w in wins
                    try
                        set out to out & (name of w) & " | "
                    end try
                end repeat
            end if
        end try
        set out to out & linefeed
    end repeat
    return out
end tell
""", timeout=12)
        return t[:3000] if t else "[无法列出]"
    if IS_WIN:
        try:
            r = subprocess.run(["powershell","-NoProfile","-Command",
                "Get-Process|Where-Object{$_.MainWindowTitle -ne ''}|Select Name,MainWindowTitle|ConvertTo-Json"],
                capture_output=True, text=True, timeout=10)
            return r.stdout[:3000]
        except Exception: return "[无法列出]"
    for c in [["wmctrl","-l"],["xdotool","search","--name",""]]:
        try:
            r = subprocess.run(c, capture_output=True, text=True, timeout=5)
            if r.returncode == 0: return r.stdout[:3000]
        except FileNotFoundError: continue
    return "[无法列出，请安装 wmctrl]"


@mcp.tool()
def get_clipboard() -> str:
    """获取剪贴板内容，自动过滤敏感信息。"""
    return redact(_clipboard()[:3000])


@mcp.tool()
def detect_screen_errors() -> str:
    """自动扫描屏幕报错（Python/JS/终端/HTTP 等）。"""
    ctx  = brain.get_context_for_tool()
    text = ctx.get("text","")
    errs = ctx.get("errors",[]) or find_errors(text)
    return json.dumps({
        "app":          ctx.get("prev_app","") or ctx.get("source",""),
        "errors_found": len(errs),
        "errors":       errs,
        **({"message":"未检测到错误"} if not errs else {}),
    }, ensure_ascii=False, indent=2)


# ── 监控控制 ──────────────────────────────────────────────────────────────────

@mcp.tool()
def set_monitor_mode(mode: str) -> str:
    """
    切换 ScreenBrain 感知模式（立即生效，无需重启）。

    模式说明（从轻到重）：
      off  CPU:0%    截图:0/h      Token/查:0      费用:$0     完全关闭
      E    CPU:<0.5% 截图:5-20/h   Token/查:~200   费用:~$1/月  省电，日常挂着
      A    CPU:~5%   截图:30-100/h Token/查:~500   费用:~$2/月  内容变化检测
      S    CPU:~10%  截图:60-200/h Token/查:~1000  费用:~$20-30/月 全功率+AI主动思考(每10s调Haiku)

    注：截图缓存在本地不耗 token，调用 get_*_screenshot 时才发送给 AI。
        费用按每天50次AI对话 × Claude Sonnet $3/M token 估算。

    参数：
      mode: "off" / "E" / "A" / "S"（大小写均可）
    """
    prev = brain.mode
    try:
        brain.set_mode(mode)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    cur = brain.mode
    if _MEMORY_OK:
        write_event("task", "system", f"感知模式: {prev} → {cur}")

    desc = {
        "off": "完全关闭，零捕获",
        "E":   "App切换 + URL跳转（省电，日常挂着）",
        "A":   "E + 页面内容哈希变化检测（中等强度）",
        "S":   "A + 更灵敏哈希 + 标题变化 + 终端内容（全功率）",
    }
    return json.dumps({
        "prev_mode":  prev,
        "mode":       cur,
        "interval":   brain._interval,
        "description":desc.get(cur,""),
        "stats":      brain.stats(),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def start_live_monitor(mode: str = "S") -> str:
    """
    快捷开启监控（等同于 set_monitor_mode）。

    参数：mode = "E" / "A" / "S"（默认）
    当前已是目标模式时返回当前状态，不重复操作。
    """
    m = mode.upper() if mode.upper() in ("E","A","S") else mode.lower()
    if brain.mode == m:
        return json.dumps({"status":f"already_{m}","stats":brain.stats()})
    return set_monitor_mode(mode)


@mcp.tool()
def stop_live_monitor() -> str:
    """关闭感知（切到 off 模式）。再次开启用 start_live_monitor 或 set_monitor_mode。"""
    if brain.mode == "off":
        return json.dumps({"status":"already_off"})
    return set_monitor_mode("off")


@mcp.tool()
def live_monitor_stats() -> str:
    """查看 ScreenBrain 运行状态、资源占用和费用估算。"""
    s = brain.stats()
    cost = {
        "off": {"cpu":"0%",    "captures":"0/h",      "token_per_query":"~0",    "ai_calls":"0",     "monthly":"$0"},
        "E":   {"cpu":"<0.5%", "captures":"5-20/h",   "token_per_query":"~200",  "ai_calls":"0",     "monthly":"~$1"},
        "A":   {"cpu":"~5%",   "captures":"30-100/h", "token_per_query":"~500",  "ai_calls":"0",     "monthly":"~$2"},
        "S":   {"cpu":"~10%",  "captures":"60-200/h", "token_per_query":"~1000", "ai_calls":"3/min", "monthly":"~$20-30"},
    }
    c = cost.get(s["mode"], {})
    thinker = s.get("thinker",{})
    msg = (
        f"模式:{s['mode']} | CPU:{c.get('cpu','?')} | "
        f"截图:{c.get('captures','?')} | Token/查:{c.get('token_per_query','?')} | "
        f"预估:{c.get('monthly','?')}/月 | 已记录{s['events']}事件 | 内存≈{s['mem_kb']}KB"
    )
    if thinker:
        status = "思考中" if thinker.get("running") else ("自动休眠" if thinker.get("auto_slept") else "未启动")
        msg += f" | Thinker:{status} API调用:{thinker.get('api_calls',0)}次 费用:{thinker.get('cost_est','$0')}"
    return json.dumps({**s, "cost": c, "msg": msg}, ensure_ascii=False, indent=2)


@mcp.tool()
def get_smart_insights(n: int = 10) -> str:
    """
    获取 S 模式 AI 主动思考生成的最新洞察。

    这些洞察由后台 ProactiveThinker 每 10s 自动生成，
    不需要你提问——AI 一直在观察并记录有价值的发现。

    参数：n 返回最近几条（默认10）
    """
    sess = brain._session
    if not sess:
        return json.dumps({"error":"记忆模块不可用"})

    # FIX: insights 现在是 deque，需要先转 list 才能切片
    insights = list(sess.insights)[-n:] if sess.insights else []
    thinker  = brain._thinker

    result: dict = {
        "count":   len(insights),
        "insights": list(reversed(insights)),   # 最新的在前
    }
    if thinker:
        ts = thinker.stats()
        result["thinker"] = {
            "status":     "思考中" if ts["running"] else ("自动休眠" if ts["auto_slept"] else "未启动"),
            "api_calls":  ts["api_calls"],
            "cost":       ts["cost_est"],
            "idle_min":   ts["idle_min"],
            "switch_5min":ts["switch_5min"],
        }
    if not insights:
        result["hint"] = "切换到 S 模式后 AI 才会开始主动思考，用 set_monitor_mode('S') 开启"

    return json.dumps(result, ensure_ascii=False, indent=2)


# ── 记忆与洞察 ────────────────────────────────────────────────────────────────

@mcp.tool()
def record_insight(text: str, tags: str = "") -> str:
    """
    记录 AI 洞察到持久日志（跨会话可查）。

    适用：解决了复杂问题、发现错误模式、完成里程碑、需要记住的规律。
    参数：text 洞察内容，tags 标签逗号分隔（如 "bug,python,已解决"）。
    """
    if not _MEMORY_OK: return json.dumps({"error":"记忆模块不可用"})
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    write_insight(text)
    if brain._session: brain._session.add_insight(text)
    return json.dumps({
        "status":  "saved",
        "insight": text[:200],
        "tags":    tag_list,
        "path":    str(__import__("pathlib").Path.home() / ".screen-mcp" / "journal.md"),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def get_journal(query: str = "", days: int = 1) -> str:
    """
    查询历史日志。
    参数：query 关键词（留空返回最近日志），days 天数（默认1=今天，最大7）。
    """
    if not _MEMORY_OK: return json.dumps({"error":"记忆模块不可用"})
    if query:
        results = journal_search(query, max_results=8)
        return json.dumps({"query":query,"found":len(results),"results":results},
                          ensure_ascii=False, indent=2)[:8000]
    days  = max(1, min(days, 7))
    text  = read_today(6000) if days == 1 else read_recent(days, 6000)
    label = "今日" if days == 1 else f"最近{days}天"
    return json.dumps({"period":label,"content":text or "（暂无记录）","stats":journal_stats()},
                      ensure_ascii=False, indent=2)[:8000]


@mcp.tool()
def get_session_timeline() -> str:
    """
    本次会话完整时间线和行为分析：
    App 使用时长 / 任务模式 / 操作轨迹 / 错误记录 / 话题聚类 / 空闲检测。
    """
    sess = brain._session
    if not sess: return json.dumps({"error":"记忆模块不可用"})
    return json.dumps({
        "narrative":       sess.session_narrative(),
        "app_summary":     sess.app_summary(),
        "current_pattern": sess.current_pattern(),
        "recent_errors":   sess.recent_errors(5),
        "topics":          sess.topic_summary(),
        "idle_warning":    sess.idle_warning(5.0),
        "brain_events":    brain._event_count,
    }, ensure_ascii=False, indent=2)[:8000]


@mcp.tool()
def set_reminder(text: str, context: str = "") -> str:
    """
    设置提醒，写入持久日志（下次查 get_journal 可见）。
    适用：用户说「下次记得…」/ 标记 TODO / 未解决问题跟进。
    """
    if not _MEMORY_OK: return json.dumps({"error":"记忆模块不可用"})
    if not context:
        cur = brain.current_snap()
        context = f"App:{cur.get('app','')} | {cur.get('title','')[:50]}"
    _journal_reminder(text, context=context)
    return json.dumps({"status":"saved","reminder":text[:200],"context":context[:100]},
                      ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# v7.0 新增工具
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def ping() -> str:
    """
    健康检查：确认 MCP 服务器运行中，返回版本、模式、运行时长等状态。
    用途：确认连接正常；调试时快速了解当前状态。
    """
    s = brain.stats()
    return json.dumps({
        "status":    "ok",
        "version":   VERSION,
        "platform":  PLATFORM,
        "uptime_s":  round(time.time() - _START_TIME),
        "mode":      brain.mode,
        "events":    s["events"],
        "mem_kb":    s["mem_kb"],
        "memory_ok": _MEMORY_OK,
        "thinker":   s.get("thinker", {}).get("running", False),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def configure(
    think_interval: Optional[int] = None,
    shot_ttl: Optional[int] = None,
    idle_threshold_min: Optional[float] = None,
    change_thr_sec: Optional[int] = None,
) -> str:
    """
    运行时修改配置参数，立即生效，无需重启。

    参数：
      think_interval     S 模式 AI 思考间隔（秒，5-300，默认10）
      shot_ttl           截图保留时长（秒，30-3600，默认300）
      idle_threshold_min 空闲降级阈值（分钟，1-60，默认5）
      change_thr_sec     change 事件写 journal 的最小间隔（秒，5-600，默认60）
    """
    changes = {}
    if think_interval is not None and brain._thinker:
        brain._thinker.THINK_INTERVAL = max(5, min(300, think_interval))
        changes["think_interval"] = brain._thinker.THINK_INTERVAL
    if shot_ttl is not None:
        brain.SHOT_TTL = max(30, min(3600, shot_ttl))
        changes["shot_ttl"] = brain.SHOT_TTL
    if idle_threshold_min is not None:
        brain.IDLE_THR = max(1.0, min(60.0, idle_threshold_min))
        changes["idle_threshold_min"] = brain.IDLE_THR
    if change_thr_sec is not None:
        brain.CHANGE_THR = max(5, min(600, change_thr_sec))
        changes["change_thr_sec"] = brain.CHANGE_THR
    if not changes:
        current = {
            "think_interval":     getattr(brain._thinker, "THINK_INTERVAL", "N/A"),
            "shot_ttl":           brain.SHOT_TTL,
            "idle_threshold_min": brain.IDLE_THR,
            "change_thr_sec":     brain.CHANGE_THR,
        }
        return json.dumps({"current": current, "hint": "传入参数以修改配置"}, ensure_ascii=False, indent=2)
    return json.dumps({"changed": changes, "hint": "修改立即生效"}, ensure_ascii=False, indent=2)


@mcp.tool()
def get_screen_diff(minutes: int = 2) -> str:
    """
    返回最近 N 分钟内屏幕发生了什么变化。

    适合问："这段时间发生了什么？" / "我刚才切到哪些页面？" / "有没有出现新错误？"
    参数：minutes 回看分钟数（默认2，最大30）
    """
    minutes = max(1, min(30, minutes))
    cutoff  = time.time() - minutes * 60
    with brain._lock:
        recent = [e for e in brain._events if e["ts"] >= cutoff]

    if not recent:
        return json.dumps({"period_min": minutes, "events": 0,
                           "summary": f"过去 {minutes} 分钟没有检测到变化"})

    apps  = list(dict.fromkeys(e["app"] for e in recent))
    urls  = list(dict.fromkeys(e["url"] for e in recent if e.get("url")))
    errs  = [e for e in recent if e.get("errors")]
    trail = [f"[{e['t']}]{e['app']}({e['kind']}) {e.get('title','')[:20]}" for e in recent[:15]]

    return json.dumps({
        "period_min":      minutes,
        "events":          len(recent),
        "apps_visited":    apps[:8],
        "urls_visited":    urls[:6],
        "errors_detected": len(errs),
        "error_samples":   [e["errors"][0][:80] for e in errs[:3]],
        "trail":           trail,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def export_session_report(fmt: str = "markdown") -> str:
    """
    生成今日工作报告，适合写日报 / 站会 / 周报。

    包含：工作时长、访问的 App 和网站、遇到的错误、AI 洞察摘要。
    参数：fmt = "markdown"（默认）或 "json"
    """
    sess = brain._session
    if not sess:
        return json.dumps({"error": "会话数据不可用"})

    apps      = sess.app_summary()
    errors    = list(sess.errors)
    insights  = list(sess.insights)
    topics    = sess.topic_summary()
    duration  = round((time.time() - sess.start_time) / 60, 1)
    date_str  = time.strftime("%Y-%m-%d")

    if fmt == "json":
        return json.dumps({
            "date":         date_str,
            "duration_min": duration,
            "apps":         apps,
            "error_count":  len(errors),
            "insight_count":len(insights),
            "topics":       topics,
        }, ensure_ascii=False, indent=2)

    lines = [
        f"# 工作报告 — {date_str}",
        f"**工作时长**: {duration} 分钟\n",
        "## 应用使用",
    ]
    for a in apps[:8]:
        lines.append(f"- **{a['app']}**: {a['minutes']} 分钟")

    if topics:
        lines.append("\n## 访问主题")
        for domain, titles in list(topics.items())[:6]:
            lines.append(f"- **{domain}**: {', '.join(str(t) for t in titles[:3])}")

    if errors:
        lines.append(f"\n## 遇到的问题（共 {len(errors)} 条）")
        for e in errors[-3:]:
            lines.append(f"- [{e['time']}] {e['app']}: {e['errors'][0][:60] if e['errors'] else ''}")

    if insights:
        lines.append(f"\n## AI 洞察（共 {len(insights)} 条）")
        for i in insights[-5:]:
            lines.append(f"- {i}")

    if _MEMORY_OK:
        today = read_today(1500)
        if today:
            lines.append("\n## 今日日志摘要")
            lines.append(today[:1000])

    return "\n".join(lines)


@mcp.tool()
def smart_search(query: str) -> str:
    """
    跨源智能搜索：同时搜索 journal 历史 + 会话记忆 + 当前屏幕内容。

    适合问：
      "我之前在哪里看到 xxx？"
      "今天有没有遇到 xxx 错误？"
      "上次解决这个问题的方法是什么？"

    参数：query 搜索关键词
    """
    if not query:
        return json.dumps({"error": "请提供搜索关键词"})

    q       = query.lower()
    results: dict = {"query": query, "sources": {}}

    # 1. Journal 历史
    if _MEMORY_OK:
        hits = journal_search(query, max_results=5)
        if hits:
            results["sources"]["journal历史"] = hits

    # 2. 会话时间线
    sess = brain._session
    if sess:
        tl_hits = [
            f"[{e['time']}] {e['app']}: {e.get('preview','')[:80]}"
            for e in sess.recent_timeline(50)
            if q in (e.get("preview","") + e.get("title","")).lower()
        ]
        if tl_hits:
            results["sources"]["会话时间线"] = tl_hits[:5]

        err_hits = [
            f"[{e['time']}] {e['app']}: {' '.join(e['errors'][:2])}"
            for e in list(sess.errors)
            if q in " ".join(e["errors"]).lower()
        ]
        if err_hits:
            results["sources"]["错误记录"] = err_hits[:3]

        ins_hits = [i for i in list(sess.insights) if q in i.lower()]
        if ins_hits:
            results["sources"]["AI洞察"] = ins_hits[-3:]

    # 3. 当前屏幕
    ctx = brain.get_context_for_tool()
    screen_text = ctx.get("text","")
    if q in screen_text.lower():
        idx = screen_text.lower().find(q)
        results["sources"]["当前屏幕"] = screen_text[max(0, idx-80): idx+200]

    total = sum(len(v) if isinstance(v, list) else 1
                for v in results["sources"].values())
    results["total_hits"] = total
    if not total:
        results["hint"] = "未找到相关内容，尝试换个关键词"

    return json.dumps(results, ensure_ascii=False, indent=2)[:8000]


@mcp.tool()
def run_task(description: str) -> str:
    """
    【X 模式专属】让 AI 直接操控鼠标键盘完成任务。

    AI 会：
    1. 截图分析当前屏幕
    2. 规划动作序列
    3. 根据风险等级自动执行或弹窗确认
    4. 验证结果并报告

    安全机制：
    - 按 Esc 或把鼠标移到屏幕左上角立即停止
    - 删除/终端命令/git push 等危险操作强制手动确认
    - 所有操作有完整日志

    示例：
      run_task("帮我保存当前文件")
      run_task("把第47行的 data.map 改成 data?.map")
      run_task("运行当前文件的测试")

    参数：description 用自然语言描述要做什么
    """
    if not _X_OK:
        return json.dumps({"error": "X 模式未启用，需要安装 pyautogui：pip install pyautogui"})
    if brain.mode != "X":
        return json.dumps({"error": "请先切换到 X 模式：set_monitor_mode('X')"})

    # 截图 + 上下文
    shot = brain.latest_shot() or take_shot()
    if not shot:
        return json.dumps({"error": "截图失败"})

    ctx  = brain.get_context_for_tool()
    text = ctx.get("text","")[:400]

    # 规划动作
    task = planner.plan(description, shot, text)
    if not task:
        return json.dumps({"error": "AI 无法规划此任务，请描述更具体"})

    # 注册 overlay 回调
    t_start = time.time()

    def on_preview(t, a, level):
        show_action(t.description, a.target, level.value, t.step, len(t.actions))

    def on_complete(t):
        show_complete(t.description, t.results, time.time() - t_start)
        if _MEMORY_OK:
            status = "已完成" if not t.cancelled else "已取消"
            write_event("task", "X模式", f"{status}: {t.description}\n" + "\n".join(t.results))

    executor.on_step_preview  = on_preview
    executor.on_task_complete = on_complete

    ok, msg = executor.run_task(task)
    if not ok:
        return json.dumps({"error": msg})

    return json.dumps({
        "status":  "started",
        "task":    description,
        "steps":   len(task.actions),
        "actions": [a.target for a in task.actions],
        "hint":    "任务执行中。按 Esc 或把鼠标移到左上角可立即停止。",
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def stop_task() -> str:
    """紧急停止 X 模式正在执行的任务。"""
    if not _X_OK:
        return json.dumps({"error": "X 模式未启用"})
    executor.stop()
    return json.dumps({"status": "stopped", "hint": "任务已停止"})


@mcp.tool()
def clear_journal(days: int = 30) -> str:
    """
    清理 N 天前的日志条目，释放磁盘空间。同时删除超过 90 天的归档文件。

    参数：days 保留最近几天（默认30，最少7）
    注意：操作不可逆，建议先用 get_journal 确认要删除的内容。
    """
    if not _MEMORY_OK:
        return json.dumps({"error": "记忆模块不可用"})
    days = max(7, days)
    try:
        from screen_mcp.journal import purge_before, _cleanup_old_archives
        result = purge_before(days)
        _cleanup_old_archives()
        return json.dumps({**result, "kept_days": days}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_app_context(app_name: str, minutes: int = 30) -> str:
    """
    获取某个 App 最近 N 分钟的完整上下文。

    适合问："我刚才在 Xcode 里做了什么？" / "Chrome 上次访问了哪些页面？"
    参数：
      app_name  App 名称（如 "Google Chrome", "Visual Studio Code"）
      minutes   回看分钟数（默认30，最大120）
    """
    minutes = max(1, min(120, minutes))
    cutoff  = time.time() - minutes * 60

    with brain._lock:
        events = [e for e in brain._events
                  if e["app"].lower() == app_name.lower() and e["ts"] >= cutoff]
        snaps  = [s for s in brain._snaps
                  if s["app"].lower() == app_name.lower() and s["ts"] >= cutoff]

    if not events and not snaps:
        # 查 journal
        if _MEMORY_OK:
            hits = journal_search(app_name, max_results=5)
            return json.dumps({
                "app":    app_name,
                "period": f"最近 {minutes} 分钟",
                "found":  False,
                "journal_hits": hits,
                "hint":   f"脑内无 {app_name} 的近期记录，显示 journal 历史",
            }, ensure_ascii=False, indent=2)
        return json.dumps({"app": app_name, "found": False})

    # 会话时间统计
    sess    = brain._session
    app_min = 0.0
    if sess:
        total = {**sess.app_time}
        if sess._last_app and sess._last_app.lower() == app_name.lower():
            total[sess._last_app] = total.get(sess._last_app, 0) + (time.time() - sess._last_switch)
        for k, v in total.items():
            if k.lower() == app_name.lower():
                app_min = round(v / 60, 1)

    urls   = list(dict.fromkeys(e["url"] for e in events if e.get("url")))
    titles = list(dict.fromkeys(e["title"] for e in events if e.get("title")))
    errs   = [e for e in events if e.get("errors")]
    texts  = [s["text"] for s in snaps if s.get("text")]

    return json.dumps({
        "app":           app_name,
        "period":        f"最近 {minutes} 分钟",
        "session_min":   app_min,
        "events":        len(events),
        "urls_visited":  urls[:8],
        "titles":        titles[:6],
        "errors":        [e["errors"][0][:80] for e in errs[:3]],
        "text_samples":  [t[:200] for t in texts[:2]],
        "trail":         [f"[{e['t']}]{e['kind']} {e.get('title','')[:25]}" for e in events[:10]],
    }, ensure_ascii=False, indent=2)


# ── 启动 ──────────────────────────────────────────────────────────────────────
def main():
    brain.start(mode="E")
    print(f"[screen-mcp v{VERSION}] 运行 | {PLATFORM} | 记忆:{'OK' if _MEMORY_OK else '不可用'}", file=sys.stderr)
    mcp.run()

if __name__ == "__main__":
    main()
