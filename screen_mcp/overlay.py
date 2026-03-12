"""
Overlay — 高科技 HUD 弹窗
macOS 原生 NSPanel + NSVisualEffectView（真正的磨砂玻璃）
在独立进程运行，通过本地 socket 接收指令
"""
from __future__ import annotations
import sys, json, threading, time, socket, os
from typing import Optional

# ── 可用性检查 ────────────────────────────────────────────────────────────────
try:
    import objc
    from Foundation import NSObject, NSTimer, NSRunLoop, NSDefaultRunLoopMode
    from AppKit import (
        NSApplication, NSApp, NSPanel, NSWindow, NSView,
        NSVisualEffectView, NSTextField, NSButton, NSProgressIndicator,
        NSColor, NSFont, NSMakeRect, NSMakePoint, NSMakeSize,
        NSFloatingWindowLevel, NSWindowStyleMaskBorderless,
        NSBackingStoreBuffered, NSBlendingModeBehindWindow,
        NSViewWidthSizable, NSViewMinYMargin,
        NSTitledWindowMask, NSClosableWindowMask,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary,
    )
    _OBJC_OK = True
except ImportError:
    _OBJC_OK = False

SOCKET_PATH = "/tmp/screen-mcp-overlay.sock"
OVERLAY_PORT = 59382   # fallback TCP port


# ── 消息协议 ──────────────────────────────────────────────────────────────────
def _send_overlay(msg: dict) -> bool:
    """向 overlay 进程发送消息（从主进程调用）"""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(SOCKET_PATH)
        s.sendall((json.dumps(msg) + "\n").encode())
        s.close()
        return True
    except Exception:
        return False


def show_insight(text: str, duration: int = 8):
    """显示洞察卡片"""
    _send_overlay({"type": "insight", "text": text, "duration": duration})


def show_error(text: str, analysis: str = ""):
    """显示错误警报"""
    _send_overlay({"type": "error", "text": text, "analysis": analysis})


def show_action(task_name: str, action_name: str, level: int, step: int, total: int,
                countdown: int = 3):
    """显示动作预告卡"""
    _send_overlay({
        "type": "action", "task": task_name, "action": action_name,
        "level": level, "step": step, "total": total, "countdown": countdown,
    })


def show_status(mode: str, pattern: str = "", errors: int = 0):
    """更新常驻状态条"""
    _send_overlay({"type": "status", "mode": mode, "pattern": pattern, "errors": errors})


def show_complete(task_name: str, results: list[str], seconds: float):
    """显示任务完成报告"""
    _send_overlay({"type": "complete", "task": task_name,
                   "results": results, "seconds": seconds})


def hide():
    """隐藏所有弹窗"""
    _send_overlay({"type": "hide"})


# ── Overlay 进程入口 ──────────────────────────────────────────────────────────
def run_overlay_process():
    """
    在独立进程中启动 overlay 窗口（需要 macOS + PyObjC）。
    主进程通过 Unix socket 发送消息控制显示内容。
    """
    if not _OBJC_OK:
        print("[overlay] PyObjC 不可用，弹窗功能关闭", file=sys.stderr)
        _run_fallback()
        return

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)   # NSApplicationActivationPolicyAccessory（不出现在 Dock）

    delegate = OverlayAppDelegate.alloc().init()
    app.setDelegate_(delegate)

    # 启动 socket 监听线程
    threading.Thread(target=_socket_server, args=(delegate,), daemon=True).start()

    app.run()


def _run_fallback():
    """无 UI 时的纯 socket 服务端（打印到 stderr）"""
    _socket_server(None)


def _socket_server(delegate):
    """监听来自主进程的消息"""
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(5)
    print("[overlay] socket 监听启动", file=sys.stderr)
    while True:
        try:
            conn, _ = server.accept()
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk: break
                data += chunk
            conn.close()
            for line in data.decode().strip().splitlines():
                try:
                    msg = json.loads(line)
                    if delegate:
                        # 在主线程执行 UI 更新
                        delegate.performSelectorOnMainThread_withObject_waitUntilDone_(
                            "handleMessage:", json.dumps(msg), False
                        )
                    else:
                        print(f"[overlay] {msg}", file=sys.stderr)
                except Exception as e:
                    print(f"[overlay] 解析错误: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[overlay] socket 错误: {e}", file=sys.stderr)
            time.sleep(0.1)


# ── macOS 原生 UI ─────────────────────────────────────────────────────────────
if _OBJC_OK:

    class OverlayAppDelegate(NSObject):

        def init(self):
            self = super().init()
            if self is None: return None
            self._status_panel  = None
            self._card_panel    = None
            self._card_timer    = None
            self._init_status_bar()
            return self

        # ── 常驻状态条 ────────────────────────────────────────────────────────

        def _init_status_bar(self):
            W, H = 240, 32
            screen_w = 1440   # 默认，实际从屏幕获取
            try:
                import AppKit
                screen = AppKit.NSScreen.mainScreen()
                frame  = screen.frame()
                screen_w = int(frame.size.width)
            except Exception:
                pass

            rect  = NSMakeRect(screen_w - W - 20, 20, W, H)
            panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                rect,
                NSWindowStyleMaskBorderless,
                NSBackingStoreBuffered,
                False,
            )
            panel.setLevel_(NSFloatingWindowLevel + 1)
            panel.setOpaque_(False)
            panel.setBackgroundColor_(NSColor.clearColor())
            panel.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces |
                NSWindowCollectionBehaviorStationary
            )
            panel.setIgnoresMouseEvents_(False)
            panel.setMovableByWindowBackground_(True)

            # 磨砂玻璃背景
            effect = NSVisualEffectView.alloc().initWithFrame_(
                NSMakeRect(0, 0, W, H)
            )
            effect.setMaterial_(2)   # NSVisualEffectMaterialDark
            effect.setBlendingMode_(NSBlendingModeBehindWindow)
            effect.setWantsLayer_(True)
            effect.layer().setCornerRadius_(10)
            effect.layer().setMasksToBounds_(True)

            # 状态文字
            self._status_label = NSTextField.alloc().initWithFrame_(
                NSMakeRect(10, 6, W - 20, 20)
            )
            self._status_label.setBezeled_(False)
            self._status_label.setDrawsBackground_(False)
            self._status_label.setEditable_(False)
            self._status_label.setSelectable_(False)
            self._status_label.setStringValue_("◉ E  |  ready")
            self._status_label.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(
                0.6, 0.85, 1.0, 1.0
            ))
            self._status_label.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11, 0.0))

            effect.addSubview_(self._status_label)
            panel.contentView().addSubview_(effect)
            panel.makeKeyAndOrderFront_(None)
            panel.orderFrontRegardless()

            self._status_panel = panel

        def _update_status(self, text: str, color_r=0.6, color_g=0.85, color_b=1.0):
            if self._status_label:
                self._status_label.setStringValue_(text)
                self._status_label.setTextColor_(
                    NSColor.colorWithRed_green_blue_alpha_(color_r, color_g, color_b, 1.0)
                )

        # ── 洞察卡片 ──────────────────────────────────────────────────────────

        def _show_card(self, title: str, body: str, duration: int = 8,
                       accent=(0.0, 0.67, 1.0)):
            if self._card_panel:
                self._card_panel.orderOut_(None)
                self._card_panel = None
            if self._card_timer:
                self._card_timer.invalidate()
                self._card_timer = None

            W, H = 320, 120
            screen_h = 800
            try:
                import AppKit
                frame = AppKit.NSScreen.mainScreen().frame()
                screen_h = int(frame.size.height)
            except Exception:
                pass

            rect  = NSMakeRect(20, screen_h - H - 60, W, H)
            panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False,
            )
            panel.setLevel_(NSFloatingWindowLevel + 2)
            panel.setOpaque_(False)
            panel.setBackgroundColor_(NSColor.clearColor())
            panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces)

            effect = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
            effect.setMaterial_(2)
            effect.setBlendingMode_(NSBlendingModeBehindWindow)
            effect.setWantsLayer_(True)
            effect.layer().setCornerRadius_(12)
            effect.layer().setMasksToBounds_(True)
            # 彩色边框
            r, g, b = accent
            effect.layer().setBorderColor_(
                NSColor.colorWithRed_green_blue_alpha_(r, g, b, 0.6).CGColor()
            )
            effect.layer().setBorderWidth_(1.0)

            # 标题
            title_lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(12, H-28, W-24, 18))
            title_lbl.setBezeled_(False)
            title_lbl.setDrawsBackground_(False)
            title_lbl.setEditable_(False)
            title_lbl.setStringValue_(title)
            title_lbl.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(r, g, b, 1.0))
            title_lbl.setFont_(NSFont.boldSystemFontOfSize_(11))

            # 正文
            body_lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 32, W-24, H-56))
            body_lbl.setBezeled_(False)
            body_lbl.setDrawsBackground_(False)
            body_lbl.setEditable_(False)
            body_lbl.setStringValue_(body[:200])
            body_lbl.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(0.88, 0.92, 1.0, 1.0))
            body_lbl.setFont_(NSFont.systemFontOfSize_(11))
            body_lbl.setWraps_(True)

            # 倒计时进度条
            prog = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(12, 10, W-24, 4))
            prog.setStyle_(0)   # NSProgressIndicatorStyleBar
            prog.setIndeterminate_(False)
            prog.setMinValue_(0)
            prog.setMaxValue_(duration)
            prog.setDoubleValue_(duration)
            self._card_progress = prog
            self._card_duration = duration
            self._card_start    = time.time()

            effect.addSubview_(title_lbl)
            effect.addSubview_(body_lbl)
            effect.addSubview_(prog)
            panel.contentView().addSubview_(effect)
            panel.makeKeyAndOrderFront_(None)
            panel.orderFrontRegardless()
            self._card_panel = panel

            # 倒计时 timer
            self._card_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.1, self, "tickCard:", None, True
            )

        def tickCard_(self, timer):
            if not self._card_panel:
                timer.invalidate()
                return
            elapsed = time.time() - self._card_start
            remaining = self._card_duration - elapsed
            if remaining <= 0:
                self._card_panel.orderOut_(None)
                self._card_panel = None
                timer.invalidate()
                self._card_timer = None
                return
            if self._card_progress:
                self._card_progress.setDoubleValue_(remaining)

        # ── 消息处理 ──────────────────────────────────────────────────────────

        def handleMessage_(self, msg_json: str):
            try:
                msg = json.loads(msg_json)
            except Exception:
                return

            t = msg.get("type", "")

            if t == "status":
                mode    = msg.get("mode","E")
                pattern = msg.get("pattern","")
                errors  = msg.get("errors", 0)
                icons   = {"off":"○","E":"◉","A":"◈","S":"⬡","X":"⚡"}
                icon    = icons.get(mode, "◉")
                err_str = f"  ⚠ {errors}" if errors else ""
                text    = f"{icon} {mode}  |  {pattern or 'ready'}{err_str}"
                if errors:
                    self._update_status(text, 1.0, 0.4, 0.3)
                elif mode == "S":
                    self._update_status(text, 0.4, 1.0, 0.6)
                elif mode == "X":
                    self._update_status(text, 1.0, 0.8, 0.0)
                else:
                    self._update_status(text)

            elif t == "insight":
                self._show_card(
                    "🤖 AI 观察",
                    msg.get("text",""),
                    msg.get("duration", 8),
                    accent=(0.0, 0.67, 1.0),
                )

            elif t == "error":
                body = msg.get("text","")
                if msg.get("analysis"):
                    body += f"\n\n→ {msg['analysis']}"
                self._show_card("⚠️ 检测到问题", body, 12, accent=(1.0, 0.3, 0.3))
                self._update_status("⚡ 错误  |  分析中", 1.0, 0.4, 0.3)

            elif t == "action":
                step  = msg.get("step", 1)
                total = msg.get("total", 1)
                body  = (
                    f"任务：{msg.get('task','')}\n"
                    f"步骤 {step}/{total}\n\n"
                    f"即将执行：{msg.get('action','')}"
                )
                cd = msg.get("countdown", 3)
                level = msg.get("level", 2)
                accent = (0.0, 0.67, 1.0) if level < 3 else (1.0, 0.6, 0.0)
                self._show_card(f"⚡ AI 准备操作", body, cd, accent=accent)

            elif t == "complete":
                results = msg.get("results", [])
                secs    = msg.get("seconds", 0)
                body    = "\n".join(results[-5:])
                self._show_card(
                    f"✅ 任务完成  ({secs:.1f}s)",
                    body, 10, accent=(0.2, 0.9, 0.5),
                )
                self._update_status("◉ X  |  完成", 0.2, 0.9, 0.5)

            elif t == "hide":
                if self._card_panel:
                    self._card_panel.orderOut_(None)
                    self._card_panel = None


# ── 从命令行直接运行 overlay 进程 ────────────────────────────────────────────
if __name__ == "__main__":
    run_overlay_process()
