"""
Claude Code HUD - 悬浮球监控器
依赖: PyQt5, websockets
启动: python hud.py
"""
from __future__ import annotations  # 兼容 Python 3.9

import sys
import json
import math
import asyncio
import threading
import queue
import uuid
import os
import time
import socket
import logging
import ctypes
import ctypes.wintypes
from logging.handlers import RotatingFileHandler
from datetime import datetime

# asyncio 线程 -> Qt 主线程的消息队列
_msg_queue: queue.Queue = queue.Queue()

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
STATE_FILE      = os.path.join(BASE_DIR, "sessions.json")
HISTORY_FILE    = os.path.join(BASE_DIR, "history.json")
LOG_DIR         = os.path.join(BASE_DIR, "logs")
MAX_HISTORY     = 100
# 恢复 sessions.json 时，超过此时长未更新的会话视为已结束
STALE_SESSION_SECONDS = 3600


def _setup_logger() -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)
    lg = logging.getLogger("hud")
    lg.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "hud.log"),
        maxBytes=512 * 1024, backupCount=1, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    lg.addHandler(handler)
    return lg


logger = _setup_logger()


def _play_sound(name: str):
    """播放系统提示音（Windows 内置）"""
    try:
        import winsound
        sounds = {
            "permission": winsound.MB_ICONEXCLAMATION,
            "approved":   winsound.MB_ICONASTERISK,
            "denied":     winsound.MB_ICONHAND,
        }
        winsound.MessageBeep(sounds.get(name, winsound.MB_OK))
    except Exception:
        pass


# history 内存维护、定期落盘（高频 PostToolUse 时避免每条都全量读写文件）
_history_cache: list = []
_history_dirty = False


def _load_history():
    global _history_cache
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            _history_cache = json.load(f)
    except Exception:
        _history_cache = []


def _append_history(entry: dict):
    global _history_dirty
    _history_cache.append(entry)
    if len(_history_cache) > MAX_HISTORY:
        del _history_cache[:len(_history_cache) - MAX_HISTORY]
    _history_dirty = True


def _flush_history():
    global _history_dirty
    if not _history_dirty:
        return
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(_history_cache, f, ensure_ascii=False, indent=2)
        _history_dirty = False
    except Exception:
        logger.exception("写入 history.json 失败")


def load_persisted_sessions() -> dict:
    """恢复会话状态，丢弃长时间未更新的僵尸会话"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("读取 sessions.json 失败")
        return {}
    now = time.time()
    fresh = {
        sid: state for sid, state in data.items()
        if isinstance(state, dict)
        and now - state.get("last_update_ts", 0) < STALE_SESSION_SECONDS
    }
    dropped = len(data) - len(fresh)
    if dropped:
        logger.info("恢复会话时丢弃 %d 个过期会话", dropped)
    return fresh

from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QHBoxLayout,
    QVBoxLayout, QFrame, QScrollArea, QDialog, QTextEdit, QMenu, QAction,
    QToolTip
)
from PyQt5.QtCore import (
    Qt, QPoint, QPointF, QTimer, QPropertyAnimation, QEasingCurve,
    pyqtSignal, QObject, QThread, QRect, QRectF, QSize,
    QAbstractNativeEventFilter
)
from PyQt5.QtGui import (
    QPainter, QColor, QBrush, QPen, QFont, QFontMetrics,
    QPainterPath, QLinearGradient, QCursor
)

import websockets
from websockets.server import serve as ws_serve

WS_PORT = 17890   # 权限请求/响应（需要双向应答）
UDP_PORT = 17891  # 状态上报（fire-and-forget，hook 端零依赖）

STATUS_COLORS = {
    "idle":               QColor(90, 90, 110),     # 蓝灰 #5A5A6E
    "working":            QColor(76, 175, 80),     # 青草绿 #4CAF50
    "waiting":            QColor(255, 234, 0),     # 明艳黄 #FFEA00
    "waiting_permission": QColor(255, 179, 26),    # 明亮橙 #FFB31A
    "error":              QColor(255, 77, 109),    # 霓虹粉红 #FF4D6D
    "done":               QColor(76, 175, 80),
}

TOOL_LABELS = {
    "Bash": "执行命令",
    "Edit": "编辑文件",
    "Write": "写入文件",
    "Read": "读取文件",
    "Grep": "搜索内容",
    "Glob": "查找文件",
    "Agent": "启动子Agent",
    "WebSearch": "网络搜索",
    "WebFetch": "获取网页",
}


class EventBus(QObject):
    session_update = pyqtSignal(str, dict)
    permission_request = pyqtSignal(str, dict)
    # request_id, approved, always_allow, fallback(转原生确认)
    permission_response = pyqtSignal(str, bool, bool, bool)
    # 原生确认提醒（敏感文件/MCP 等 hook 无法替代的确认）session_id, msg
    notification = pyqtSignal(str, dict)
    # 系统级警告（UDP 绑定失败等），在悬浮球上提示
    system_warning = pyqtSignal(str)


bus = EventBus()

# session状态存储
sessions: dict[str, dict] = {}
# 等待授权的请求 request_id -> (asyncio.Future, websocket)
pending_permissions: dict[str, tuple] = {}
# 授权请求与会话的对应关系 request_id -> session_id
request_sessions: dict[str, str] = {}
# pending_permissions 被 WS 线程和 Qt 主线程并发读写，必须加锁
_perm_lock = threading.Lock()
# WebSocket连接池
ws_connections: set = set()


def short_tool_input(tool_name: str, tool_input: dict) -> str:
    """一行式操作对象摘要（状态展示/历史记录用），绝不原样输出 JSON"""
    if not isinstance(tool_input, dict):
        return str(tool_input)[:80]
    if tool_name == "Bash":
        cmd = " ".join(tool_input.get("command", "").split())
        return cmd[:80] + "..." if len(cmd) > 80 else cmd
    if tool_name == "Grep":
        return f'搜索 "{tool_input.get("pattern", "")}"'
    path = (tool_input.get("file_path") or tool_input.get("notebook_path")
            or tool_input.get("path") or "")
    if path:
        parts = str(path).replace("\\", "/").rstrip("/").split("/")
        return "/".join(parts[-2:])
    for key in ("description", "pattern", "query", "url", "skill"):
        val = tool_input.get(key)
        if val:
            val = str(val)
            return val[:60] + "..." if len(val) > 60 else val
    return ""


def full_tool_input(tool_name: str, tool_input: dict) -> str:
    """授权弹窗用：展示完整内容，不截断（安全起见必须让用户看到全部命令）"""
    if not isinstance(tool_input, dict):
        return str(tool_input)
    if tool_name == "Bash":
        return tool_input.get("command", "")
    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Read"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if tool_name == "Write":
            content = tool_input.get("content", "")
            return f"{path}\n--- 写入内容 ({len(content)} 字符) ---\n{content[:2000]}"
        if tool_name == "Edit":
            old = tool_input.get("old_string", "")
            new = tool_input.get("new_string", "")
            return f"{path}\n--- 替换 ---\n{old[:800]}\n--- 为 ---\n{new[:800]}"
        return path
    try:
        return json.dumps(tool_input, ensure_ascii=False, indent=2)[:2000]
    except Exception:
        return str(tool_input)[:2000]


def _process_msg(msg: dict):
    """在 Qt 主线程里处理消息，更新 sessions 并发信号"""
    mtype = msg.get("type", "")
    sid = msg.get("session_id", "unknown")

    if mtype == "pre_tool":
        # 会话恢复活动说明原生确认已被处理，关掉对应提醒窗
        ReminderDialog.dismiss_for(sid)
        sessions.setdefault(sid, {"status": "idle", "cwd": "", "history": []})
        sessions[sid].update({
            "status": "working",
            "tool_name": msg.get("tool_name", ""),
            "tool_input": msg.get("tool_input", {}),
            "cwd": msg.get("cwd", sessions[sid].get("cwd", "")),
            "last_update": datetime.now().strftime("%H:%M:%S"),
            "last_update_ts": time.time(),
            "tool_start_ts": time.time(),
        })
        if msg.get("term_hwnd"):
            sessions[sid]["term_hwnd"] = int(msg["term_hwnd"])
        bus.session_update.emit(sid, dict(sessions[sid]))

    elif mtype == "post_tool":
        if sid in sessions:
            is_error = bool(msg.get("is_error"))
            sessions[sid]["status"] = "error" if is_error else "waiting"
            sessions[sid]["last_update_ts"] = time.time()
            sessions[sid]["tool_start_ts"] = 0
            tool = msg.get("tool_name", "")
            history = sessions[sid].setdefault("history", [])
            entry = {"tool": tool, "time": datetime.now().strftime("%H:%M:%S"),
                     "session": sid[:8], "cwd": sessions[sid].get("cwd", "")}
            # 并行工具时 tool_input 可能已被后续 pre_tool 覆盖，工具名一致才取摘要
            if sessions[sid].get("tool_name") == tool:
                detail = short_tool_input(tool, sessions[sid].get("tool_input", {}))
                if detail:
                    entry["detail"] = detail
            if is_error:
                entry["error"] = True
            history.append(entry)
            if len(history) > 20:
                history.pop(0)
            _append_history(entry)
            bus.session_update.emit(sid, dict(sessions[sid]))

    elif mtype == "stop":
        ReminderDialog.dismiss_for(sid)
        if sid in sessions:
            sessions[sid].update({"status": "idle", "tool_name": "", "tool_input": {},
                                  "tool_start_ts": 0, "last_update_ts": time.time()})
            bus.session_update.emit(sid, dict(sessions[sid]))

    elif mtype == "session_start":
        sessions[sid] = {
            "status": "waiting", "tool_name": "", "tool_input": {},
            "cwd": msg.get("cwd", ""), "history": [],
            "term_hwnd": int(msg.get("term_hwnd") or 0),
            "last_update": datetime.now().strftime("%H:%M:%S"),
            "last_update_ts": time.time(),
            "start_ts": time.time(),
        }
        bus.session_update.emit(sid, dict(sessions[sid]))

    elif mtype == "session_end":
        ReminderDialog.dismiss_for(sid)
        sessions.pop(sid, None)
        bus.session_update.emit(sid, {"status": "removed"})

    elif mtype == "permission_request":
        req_id = msg.get("request_id", "")
        if sid in sessions:
            sessions[sid]["status"] = "waiting_permission"
            sessions[sid]["last_update_ts"] = time.time()
            bus.session_update.emit(sid, dict(sessions[sid]))
        if req_id:
            request_sessions[req_id] = sid
        threading.Thread(target=_play_sound, args=("permission",), daemon=True).start()
        bus.permission_request.emit(req_id, msg)

    elif mtype == "notification":
        # Claude Code 原生确认提示出现（敏感文件/MCP/转终端/空闲等待），HUD 弹提醒窗
        if sid in sessions:
            sessions[sid]["status"] = "waiting_permission"
            sessions[sid]["last_update_ts"] = time.time()
            bus.session_update.emit(sid, dict(sessions[sid]))
        threading.Thread(target=_play_sound, args=("permission",), daemon=True).start()
        bus.notification.emit(sid, msg)

    elif mtype == "system_warning":
        bus.system_warning.emit(msg.get("message", ""))


async def ws_handler(websocket):
    ws_connections.add(websocket)
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            if msg.get("type") == "permission_request":
                req_id = msg.get("request_id", str(uuid.uuid4()))
                loop = asyncio.get_event_loop()
                future = loop.create_future()
                with _perm_lock:
                    pending_permissions[req_id] = (future, websocket)
                _msg_queue.put(msg)
                result = await future
                with _perm_lock:
                    pending_permissions.pop(req_id, None)
                await websocket.send(json.dumps({
                    "type":         "permission_response",
                    "request_id":   req_id,
                    "approved":     result.get("approved", False),
                    "always_allow": result.get("always_allow", False),
                    "fallback":     result.get("fallback", False),
                }))
            else:
                _msg_queue.put(msg)

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        ws_connections.discard(websocket)


def on_permission_response(request_id: str, approved: bool, always_allow: bool,
                           fallback: bool = False):
    with _perm_lock:
        entry = pending_permissions.get(request_id)
        # 按 request_id 精确恢复对应会话，避免多会话同时授权时恢复错对象
        sid = request_sessions.pop(request_id, None)
    if entry is not None:
        future, ws = entry
        if sid and sessions.get(sid, {}).get("status") == "waiting_permission":
            sessions[sid]["status"] = "waiting"
            bus.session_update.emit(sid, dict(sessions[sid]))
        if not future.done():
            future.get_loop().call_soon_threadsafe(
                future.set_result,
                {"approved": approved, "always_allow": always_allow,
                 "fallback": fallback},
            )


bus.permission_response.connect(on_permission_response)


def run_ws_server(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    async def main():
        async with ws_serve(ws_handler, "localhost", WS_PORT):
            await asyncio.Future()
    loop.run_until_complete(main())


def run_udp_listener():
    """接收 notify.py 的轻量状态上报（UDP，无需握手）。
    socket 级故障（端口被占、休眠唤醒后失效）自动重建并指数退避，不让线程死掉。"""
    sock = None
    backoff = 1.0
    warned = False
    while True:
        if sock is None:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind(("127.0.0.1", UDP_PORT))
                backoff = 1.0
                warned = False
            except OSError:
                logger.exception("UDP 端口 %d 绑定失败，%.0f 秒后重试", UDP_PORT, backoff)
                if not warned:
                    _msg_queue.put({"type": "system_warning",
                                    "message": f"UDP {UDP_PORT} 绑定失败，状态上报暂不可用"})
                    warned = True
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                sock = None
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
        try:
            data, _ = sock.recvfrom(65535)
        except OSError:
            logger.exception("UDP socket 失效，重建")
            try:
                sock.close()
            except OSError:
                pass
            sock = None
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            continue
        try:
            _msg_queue.put(json.loads(data.decode("utf-8")))
        except Exception:
            logger.exception("UDP 消息处理失败")


class SessionTab(QFrame):
    """Excel sheet 标签风格的单个 session tab"""

    TAB_W = 130
    TAB_H = 72

    clicked = pyqtSignal(str)

    def __init__(self, session_id: str, parent=None):
        super().__init__(parent)
        self.session_id = session_id
        self.setFixedSize(self.TAB_W, self.TAB_H)
        self.setCursor(Qt.PointingHandCursor)
        self._active = False
        self._setup_ui()
        self._set_style(active=False)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.session_id)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(4)
        self.dot = QLabel("●")
        self.dot.setFixedWidth(14)
        self.dot.setStyleSheet("font-size: 10px;")
        self.name_label = QLabel("...")
        self.name_label.setStyleSheet("color: #D8D8E8; font-size: 11px; font-weight: bold;")
        self.name_label.setMaximumWidth(self.TAB_W - 36)
        top.addWidget(self.dot)
        top.addWidget(self.name_label, 1)

        self.action_label = QLabel("")
        self.action_label.setStyleSheet("color: #9A9AB0; font-size: 11px;")

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(0)
        self.detail_label = QLabel("")
        self.detail_label.setStyleSheet("color: #6A6A80; font-size: 10px;")
        self.detail_label.setMaximumWidth(self.TAB_W - 50)
        self.elapsed_label = QLabel("")
        self.elapsed_label.setStyleSheet("color: #5C5C78; font-size: 10px;")
        self.elapsed_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bottom_row.addWidget(self.detail_label, 1)
        bottom_row.addWidget(self.elapsed_label)

        layout.addLayout(top)
        layout.addWidget(self.action_label)
        layout.addLayout(bottom_row)
        layout.addStretch()

        self._start_ts: float = 0

    def _set_style(self, active: bool):
        self._active = active
        if active:
            self.setStyleSheet("""
                QFrame {
                    background: rgba(32,32,50,245);
                    border-top: 2px solid #00E5A0;
                    border-left: 1px solid rgba(200,200,255,0.14);
                    border-right: 1px solid rgba(200,200,255,0.07);
                    border-bottom: none;
                    border-radius: 0px;
                }
            """)
        else:
            self.setStyleSheet("""
                QFrame {
                    background: rgba(22,22,30,185);
                    border-top: 2px solid rgba(200,200,255,0.08);
                    border-left: 1px solid rgba(200,200,255,0.06);
                    border-right: 1px solid rgba(200,200,255,0.04);
                    border-bottom: none;
                    border-radius: 0px;
                }
            """)

    def update_state(self, state: dict):
        status = state.get("status", "idle")
        color = STATUS_COLORS.get(status, STATUS_COLORS["idle"])
        self.dot.setStyleSheet(f"color: {color.name()}; font-size: 10px;")

        cwd = state.get("cwd", "")
        if cwd:
            parts = cwd.replace("\\", "/").rstrip("/").split("/")
            self.name_label.setText(parts[-1] if parts else cwd)
        else:
            self.name_label.setText(self.session_id[:8])

        tool = state.get("tool_name", "")
        if tool:
            self.action_label.setText(TOOL_LABELS.get(tool, tool))
            detail = short_tool_input(tool, state.get("tool_input", {}))
            # 截短路径/命令适配宽度
            if len(detail) > 18:
                detail = detail[:16] + "…"
            self.detail_label.setText(detail)
        else:
            self.action_label.setText("空闲" if status == "idle" else "等待中")
            self.detail_label.setText("")

        # working / 等待授权时显示当前工具耗时，其余状态不计时
        if status in ("working", "waiting_permission"):
            self._start_ts = state.get("tool_start_ts") or state.get("start_ts", 0)
        else:
            self._start_ts = 0
        self.refresh_elapsed()

        is_active = status in ("working", "waiting_permission")
        if is_active != self._active:
            self._set_style(active=is_active)
        if status == "waiting_permission":
            self.action_label.setText("需要确认")
            self.action_label.setStyleSheet("color: #FF9F0A; font-size: 11px; font-weight: bold;")
            self.detail_label.setText("弹窗等待操作")
        else:
            self.action_label.setStyleSheet("color: #9A9AB0; font-size: 11px;")

    def refresh_elapsed(self):
        if self._start_ts:
            secs = int(time.time() - self._start_ts)
            if secs < 60:
                self.elapsed_label.setText(f"{secs}s")
            else:
                self.elapsed_label.setText(f"{secs//60}m{secs%60:02d}s")
        else:
            self.elapsed_label.setText("")


class _HotkeyFilter(QAbstractNativeEventFilter):
    """全局热键：权限弹窗期间 Alt+Y/N/U/Enter 决策；提醒窗期间 Esc 关闭、Alt+Enter 跳转终端。
    用 RegisterHotKey 而非键盘钩子——仅弹窗存在时注册，关闭即注销，平时不占键。
    Alt+Enter 由权限弹窗优先持有，权限热键注销后让位给提醒窗。"""
    WM_HOTKEY = 0x0312
    MOD_ALT = 0x0001
    MOD_NOREPEAT = 0x4000
    VK_ESCAPE = 0x1B
    VK_RETURN = 0x0D
    PERM_KEYS = {
        1: (0x59, "_allow"),        # Alt+Y
        2: (0x4E, "_deny"),         # Alt+N
        3: (0x55, "_always"),       # Alt+U
        4: (0x0D, "_to_terminal"),  # Alt+Enter
    }
    ESC_ID = 5
    REM_ENTER_ID = 6

    def __init__(self):
        super().__init__()
        self._perm_on = False
        self._rem_on = False
        self._rem_enter_ok = False

    def set_perm_hotkeys(self, on: bool):
        if on == self._perm_on:
            return
        user32 = ctypes.windll.user32
        if on:
            # 权限弹窗优先持有 Alt+Enter，提醒窗的注册先让位
            if self._rem_enter_ok:
                user32.UnregisterHotKey(None, self.REM_ENTER_ID)
                self._rem_enter_ok = False
            for hk_id, (vk, _) in self.PERM_KEYS.items():
                if not user32.RegisterHotKey(None, hk_id, self.MOD_ALT | self.MOD_NOREPEAT, vk):
                    logger.warning("全局热键注册失败 vk=0x%X（可能被其他程序占用）", vk)
        else:
            for hk_id in self.PERM_KEYS:
                user32.UnregisterHotKey(None, hk_id)
        self._perm_on = on
        if not on and self._rem_on:
            self._register_rem_enter()

    def set_reminder_hotkeys(self, on: bool):
        if on == self._rem_on:
            return
        user32 = ctypes.windll.user32
        if on:
            if not user32.RegisterHotKey(None, self.ESC_ID, self.MOD_NOREPEAT, self.VK_ESCAPE):
                logger.warning("Esc 全局热键注册失败（可能被其他程序占用）")
            self._rem_on = True
            self._register_rem_enter()
        else:
            user32.UnregisterHotKey(None, self.ESC_ID)
            if self._rem_enter_ok:
                user32.UnregisterHotKey(None, self.REM_ENTER_ID)
                self._rem_enter_ok = False
            self._rem_on = False

    def _register_rem_enter(self):
        # Alt+Enter 可能正被权限弹窗持有（id 4），权限热键注销时会补调本方法
        if self._rem_enter_ok or self._perm_on:
            return
        self._rem_enter_ok = bool(ctypes.windll.user32.RegisterHotKey(
            None, self.REM_ENTER_ID, self.MOD_ALT | self.MOD_NOREPEAT, self.VK_RETURN))

    @staticmethod
    def _focus_reminder_terminal():
        # 多个提醒时跳第一个（最早的）
        for sid, dlg in list(ReminderDialog._by_session.items()):
            ball = dlg.parent()
            if ball is not None and hasattr(ball, "_focus_terminal"):
                ball._focus_terminal(sid)
            return

    def nativeEventFilter(self, event_type, message):
        if (self._perm_on or self._rem_on) and event_type == b"windows_generic_MSG":
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == self.WM_HOTKEY:
                hk_id = int(msg.wParam)
                if hk_id in self.PERM_KEYS:
                    dialogs = PermissionDialog._open_dialogs
                    if dialogs:
                        # 多弹窗时作用于最早弹出的那个（等待最久的请求）
                        getattr(dialogs[0], self.PERM_KEYS[hk_id][1])()
                    return True, 0
                if hk_id == self.ESC_ID:
                    for dlg in list(ReminderDialog._by_session.values()):
                        dlg._dismiss()
                    return True, 0
                if hk_id == self.REM_ENTER_ID:
                    self._focus_reminder_terminal()
                    return True, 0
        return False, 0


_hotkey_filter = None


def _ensure_hotkey_filter() -> _HotkeyFilter:
    global _hotkey_filter
    if _hotkey_filter is None:
        _hotkey_filter = _HotkeyFilter()
        QApplication.instance().installNativeEventFilter(_hotkey_filter)
    return _hotkey_filter


class PermissionDialog(QDialog):
    TIMEOUT = 60
    # 当前打开的弹窗，用于并发请求时垂直堆叠、避免互相遮挡
    _open_dialogs: list["PermissionDialog"] = []

    def __init__(self, request_id: str, data: dict, parent=None):
        super().__init__(parent, Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.request_id = request_id
        self._always_allow = False
        self._finished = False
        self._remaining = self.TIMEOUT
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(400)
        self._setup_ui(data)

        self._countdown = QTimer(self)
        self._countdown.timeout.connect(self._tick)
        self._countdown.start(1000)

        # 位置由 FloatingBall._reposition_popups 统一锚定到悬浮球
        PermissionDialog._open_dialogs.append(self)
        PermissionDialog._refresh_hotkey_target()
        _ensure_hotkey_filter().set_perm_hotkeys(True)

    def _setup_ui(self, data: dict):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QFrame()
        self._card = card
        card.setStyleSheet("""
            QFrame {
                background: rgba(22,22,30,250);
                border-radius: 14px;
                border: 1px solid rgba(255,159,10,0.5);
            }
        """)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)

        # 标题 + 倒计时
        title_row = QHBoxLayout()
        title = QLabel("需要授权")
        title.setStyleSheet("color: #FF9F0A; font-size: 14px; font-weight: bold;")
        self._timer_label = QLabel(f"{self.TIMEOUT}s")
        self._timer_label.setStyleSheet("color: #6A6A80; font-size: 12px;")
        title_row.addWidget(title)
        title_row.addStretch()
        title_row.addWidget(self._timer_label)

        # session + 工具信息
        tool = data.get("tool_name", "未知工具")
        sid_short = data.get("session_id", "")[:8]
        cwd = data.get("cwd", "")
        cwd_short = cwd.replace("\\", "/").rstrip("/").split("/")[-1] if cwd else ""
        info_text = f"{TOOL_LABELS.get(tool, tool)}  ·  {cwd_short or sid_short}"
        info = QLabel(info_text)
        info.setStyleSheet("color: #9A9AB0; font-size: 11px;")

        # 命令详情：展示完整内容（可滚动），授权前必须能看到全部命令
        detail = full_tool_input(tool, data.get("tool_input", {}))
        detail_box = QTextEdit()
        detail_box.setReadOnly(True)
        detail_box.setPlainText(detail)
        line_count = detail.count("\n") + 1
        detail_box.setFixedHeight(min(200, max(54, line_count * 18 + 12)))
        detail_box.setLineWrapMode(QTextEdit.WidgetWidth)
        detail_box.setStyleSheet("""
            QTextEdit {
                background: rgba(110,91,255,0.10); color: #E8E8F0;
                border-radius: 6px; border: none; font-size: 11px; padding: 4px;
                font-family: Consolas, monospace;
            }
        """)

        # 按钮行
        btn_row = QHBoxLayout()
        btn_deny        = QPushButton("拒绝")
        btn_terminal    = QPushButton("转终端")
        btn_allow       = QPushButton("允许")
        btn_always_allow = QPushButton("永久允许")
        for btn in (btn_deny, btn_terminal, btn_allow, btn_always_allow):
            btn.setFixedHeight(32)
            btn.setCursor(Qt.PointingHandCursor)
        btn_deny.setStyleSheet("""
            QPushButton { background: rgba(255,77,109,0.15); color: #FF4D6D;
                border-radius: 7px; border: 1px solid rgba(255,77,109,0.4); font-size: 12px; }
            QPushButton:hover { background: rgba(255,77,109,0.32); }
        """)
        btn_terminal.setStyleSheet("""
            QPushButton { background: rgba(140,140,180,0.12); color: #9A9AB0;
                border-radius: 7px; border: 1px solid rgba(140,140,180,0.32); font-size: 12px; }
            QPushButton:hover { background: rgba(140,140,180,0.26); }
        """)
        btn_allow.setStyleSheet("""
            QPushButton { background: rgba(0,229,160,0.13); color: #00E5A0;
                border-radius: 7px; border: 1px solid rgba(0,229,160,0.4); font-size: 12px; }
            QPushButton:hover { background: rgba(0,229,160,0.3); }
        """)
        btn_always_allow.setStyleSheet("""
            QPushButton { background: rgba(110,91,255,0.15); color: #6E5BFF;
                border-radius: 7px; border: 1px solid rgba(110,91,255,0.4); font-size: 12px; }
            QPushButton:hover { background: rgba(110,91,255,0.32); }
        """)
        btn_deny.clicked.connect(self._deny)
        btn_terminal.clicked.connect(self._to_terminal)
        btn_allow.clicked.connect(self._allow)
        btn_always_allow.clicked.connect(self._always)
        btn_row.addWidget(btn_deny)
        btn_row.addWidget(btn_terminal)
        btn_row.addWidget(btn_allow)
        btn_row.addWidget(btn_always_allow)

        keys_hint = QLabel("Alt+Y 允许 · Alt+N 拒绝 · Alt+U 永久允许 · Alt+Enter 转终端")
        self._keys_hint = keys_hint
        keys_hint.setStyleSheet(
            "color: #6A6A80; font-size: 10px; background: transparent; border: none;")
        keys_hint.setAlignment(Qt.AlignCenter)

        layout.addLayout(title_row)
        layout.addWidget(info)
        layout.addWidget(detail_box)
        layout.addLayout(btn_row)
        layout.addWidget(keys_hint)

        outer.addWidget(card)
        self.adjustSize()

    @classmethod
    def _refresh_hotkey_target(cls):
        # 多弹窗垂直堆叠全部可见，热键作用于最早弹出的那个，给它亮边标识
        for i, dlg in enumerate(cls._open_dialogs):
            dlg._set_hotkey_target(i == 0)

    def _set_hotkey_target(self, on: bool):
        border = "rgba(255,179,26,0.95)" if on else "rgba(255,159,10,0.3)"
        self._card.setStyleSheet(f"""
            QFrame {{
                background: rgba(22,22,30,250);
                border-radius: 14px;
                border: 1px solid {border};
            }}
        """)
        self._keys_hint.setText(
            "Alt+Y 允许 · Alt+N 拒绝 · Alt+U 永久允许 · Alt+Enter 转终端" if on
            else "热键作用于亮边弹窗（最早弹出）")

    def _tick(self):
        self._remaining -= 1
        self._timer_label.setText(f"{self._remaining}s")
        if self._remaining <= 0:
            self._countdown.stop()
            # 超时不再直接拒绝，交回 Claude Code 原生确认（终端里无限等待）
            self._finish(approved=False, always=False, fallback=True)

    def enterEvent(self, event):
        # 用户正在查看弹窗（如阅读长命令）时暂停倒计时
        self._countdown.stop()
        self._timer_label.setText("已暂停")

    def leaveEvent(self, event):
        if self._remaining > 0:
            self._timer_label.setText(f"{self._remaining}s")
            self._countdown.start(1000)

    def _allow(self):
        self._countdown.stop()
        threading.Thread(target=_play_sound, args=("approved",), daemon=True).start()
        self._finish(approved=True, always=False)

    def _always(self):
        self._countdown.stop()
        threading.Thread(target=_play_sound, args=("approved",), daemon=True).start()
        self._finish(approved=True, always=True)

    def _deny(self):
        self._countdown.stop()
        threading.Thread(target=_play_sound, args=("denied",), daemon=True).start()
        self._finish(approved=False, always=False)

    def _to_terminal(self):
        self._countdown.stop()
        self._finish(approved=False, always=False, fallback=True)

    def reject(self):
        # Esc/系统关闭：等同"转终端"，否则 hook 收不到响应干等超时、弹窗残留堆叠列表
        if self in PermissionDialog._open_dialogs:
            self._countdown.stop()
            self._finish(approved=False, always=False, fallback=True)
        else:
            super().reject()

    def _finish(self, approved: bool, always: bool, fallback: bool = False):
        # 防重入：热键/点击可能在弹窗关闭前对同一请求触发多次
        if self._finished:
            return
        self._finished = True
        if self in PermissionDialog._open_dialogs:
            PermissionDialog._open_dialogs.remove(self)
        if not PermissionDialog._open_dialogs:
            _ensure_hotkey_filter().set_perm_hotkeys(False)
        else:
            PermissionDialog._refresh_hotkey_target()
        bus.permission_response.emit(self.request_id, approved, always, fallback)
        self.accept()
        ball = self.parent()
        if ball is not None and hasattr(ball, "_reposition_popups"):
            ball._reposition_popups()


class ReminderDialog(QDialog):
    """原生确认提醒窗：hook 无法替代的确认（敏感文件/MCP 工具/转终端/空闲等待）
    出现时提示用户去终端操作。常驻直到点击关闭，或该会话恢复活动时自动关闭。
    每个会话最多一个提醒窗，新通知更新文案而不堆叠。"""
    _by_session: dict[str, "ReminderDialog"] = {}

    def __init__(self, session_id: str, data: dict, parent=None):
        super().__init__(parent, Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.session_id = session_id
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(360)
        self._setup_ui(data)
        # 位置由 FloatingBall._reposition_popups 统一锚定到悬浮球
        ReminderDialog._by_session[session_id] = self
        _ensure_hotkey_filter().set_reminder_hotkeys(True)

    def _setup_ui(self, data: dict):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QFrame()
        card.setCursor(Qt.PointingHandCursor)
        card.setStyleSheet("""
            QFrame {
                background: rgba(22,22,30,250);
                border-radius: 14px;
                border: 1px solid rgba(94,158,255,0.55);
            }
        """)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 12)
        layout.setSpacing(6)

        title_row = QHBoxLayout()
        self._title_label = QLabel(self._title_for(data.get("message", "")))
        self._title_label.setStyleSheet(
            "color: #5E9EFF; font-size: 14px; font-weight: bold;")
        hint = QLabel("点击/Esc 关闭 · Alt+Enter 跳终端")
        hint.setStyleSheet("color: #6A6A80; font-size: 11px;")
        title_row.addWidget(self._title_label)
        title_row.addStretch()
        title_row.addWidget(hint)

        cwd = data.get("cwd", "")
        cwd_short = cwd.replace("\\", "/").rstrip("/").split("/")[-1] if cwd else ""
        sid_short = self.session_id[:8]
        info = QLabel(f"会话 {cwd_short or sid_short}")
        info.setStyleSheet("color: #9A9AB0; font-size: 11px;")

        self._msg_label = QLabel(data.get("message", "") or "Claude Code 在终端里等待你的确认")
        self._msg_label.setWordWrap(True)
        self._msg_label.setStyleSheet("color: #E8E8F0; font-size: 12px;")

        layout.addLayout(title_row)
        layout.addWidget(info)
        layout.addWidget(self._msg_label)
        outer.addWidget(card)
        self.adjustSize()

    @staticmethod
    def _title_for(message: str) -> str:
        # Notification 在原生确认和空闲等输入两种场景都会触发，按文案区分
        if "waiting for your input" in message:
            return "Claude 在等你输入"
        return "请到终端确认"

    def update_message(self, message: str):
        if message:
            self._title_label.setText(self._title_for(message))
            self._msg_label.setText(message)
            self.adjustSize()

    def mousePressEvent(self, event):
        self._dismiss()

    def reject(self):
        # Esc 也走正常注销，否则残留在 _by_session 里导致该会话提醒永远不再显示
        self._dismiss()

    def _dismiss(self):
        ReminderDialog._by_session.pop(self.session_id, None)
        if not ReminderDialog._by_session:
            _ensure_hotkey_filter().set_reminder_hotkeys(False)
        self.accept()
        ball = self.parent()
        if ball is not None and hasattr(ball, "_reposition_popups"):
            ball._reposition_popups()

    @classmethod
    def dismiss_for(cls, session_id: str):
        dlg = cls._by_session.pop(session_id, None)
        if dlg:
            dlg._dismiss()


class SessionDetailDialog(QDialog):
    """单个会话详情：状态、当前工具、最近历史"""

    def __init__(self, session_id: str, parent=None):
        super().__init__(parent, Qt.WindowStaysOnTopHint)
        state = sessions.get(session_id, {})
        cwd = state.get("cwd", "")
        proj = cwd.replace("\\", "/").rstrip("/").split("/")[-1] if cwd else session_id[:8]
        self.setWindowTitle(f"会话详情 - {proj}")
        self.resize(520, 360)
        layout = QVBoxLayout(self)
        box = QTextEdit()
        box.setReadOnly(True)
        box.setStyleSheet(
            "QTextEdit { background: #16161E; color: #D8D8E8; border: none;"
            " font-family: Consolas, monospace; font-size: 12px; }")
        lines = [
            f"会话: {session_id}",
            f"目录: {cwd or '未知'}",
            f"状态: {state.get('status', 'idle')}",
        ]
        tool = state.get("tool_name", "")
        if tool:
            lines.append(f"当前工具: {TOOL_LABELS.get(tool, tool)}")
            lines.append(f"参数: {short_tool_input(tool, state.get('tool_input', {}))}")
        lines.append("")
        lines.append("--- 最近操作 ---")
        history = state.get("history", [])
        if history:
            for e in reversed(history):
                line = f'{e.get("time", "")}  {e.get("tool", "")}'
                if e.get("detail"):
                    line += f'  {e["detail"]}'
                if e.get("error"):
                    line += " [失败]"
                lines.append(line)
        else:
            lines.append("暂无记录")
        box.setPlainText("\n".join(lines))
        layout.addWidget(box)


class HistoryDialog(QDialog):
    """最近工具调用历史（history.json）"""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowStaysOnTopHint)
        self.setWindowTitle("HUD 历史记录")
        self.resize(520, 420)
        layout = QVBoxLayout(self)
        box = QTextEdit()
        box.setReadOnly(True)
        box.setStyleSheet(
            "QTextEdit { background: #16161E; color: #D8D8E8; border: none;"
            " font-family: Consolas, monospace; font-size: 12px; }")
        hist = list(_history_cache)
        lines = []
        for e in reversed(hist):
            cwd = e.get("cwd", "")
            proj = cwd.replace("\\", "/").rstrip("/").split("/")[-1] if cwd else e.get("session", "")
            line = f'{e.get("time", "")}  {proj:<16}  {e.get("tool", "")}'
            if e.get("detail"):
                line += f'  {e["detail"]}'
            if e.get("error"):
                line += " [失败]"
            lines.append(line)
        box.setPlainText("\n".join(lines) if lines else "暂无历史记录")
        layout.addWidget(box)


class SessionPanel(QWidget):
    """Excel sheet 标签风格的横向展开面板"""

    TAB_W = SessionTab.TAB_W
    TAB_H = SessionTab.TAB_H
    PANEL_H = TAB_H + 2        # tab 高度 + 底部分隔线
    MAX_VISIBLE_TABS = 6
    EMPTY_W = 180

    def __init__(self, ball: "FloatingBall"):
        super().__init__(None)
        self._ball = ball
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._session_tabs: dict[str, SessionTab] = {}
        # None=显示全部；某 session_id=只显示该会话（悬浮单个点时）
        self._filter_sid: str | None = None
        self._build_ui()

    def set_filter(self, sid: str | None):
        if sid is not None and sid not in self._session_tabs:
            sid = None
        self._filter_sid = sid
        for tab_sid, tab in self._session_tabs.items():
            tab.setVisible(sid is None or tab_sid == sid)

    def _build_ui(self):
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        # 横向 tab 容器，带底部边框线（像 Excel）
        self._tab_bar = QWidget()
        self._tab_bar.setStyleSheet("""
            QWidget {
                background: rgba(16,16,26,235);
                border-radius: 10px 10px 0px 0px;
                border: 1px solid rgba(200,200,255,0.10);
                border-bottom: 2px solid rgba(0,229,160,0.30);
            }
        """)
        self._tabs_layout = QHBoxLayout(self._tab_bar)
        self._tabs_layout.setContentsMargins(4, 0, 4, 0)
        self._tabs_layout.setSpacing(1)
        self._tabs_layout.addStretch()

        # 无 session 时的占位
        self._empty_label = QLabel("暂无活动 Session")
        self._empty_label.setFixedSize(self.EMPTY_W, self.PANEL_H)
        self._empty_label.setStyleSheet("color: #5C5C78; font-size: 12px;")
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._tabs_layout.insertWidget(0, self._empty_label)

        outer_layout.addWidget(self._tab_bar)

    def _panel_width(self) -> int:
        n = len(self._session_tabs)
        if n == 0:
            return self.EMPTY_W + 8
        if self._filter_sid is not None:
            n = 1
        return min(n, self.MAX_VISIBLE_TABS) * (self.TAB_W + 1) + 8

    def reposition(self, ball_rect: QRect):
        w = self._panel_width()
        h = self.PANEL_H
        self._tab_bar.setFixedSize(w, h)
        self.setFixedSize(w, h)

        screen_obj = (QApplication.screenAt(ball_rect.center())
                      or QApplication.primaryScreen())
        screen = screen_obj.availableGeometry()
        # 右对齐悬浮球，向上弹出
        x = min(ball_rect.right() - w, screen.right() - w - 8)
        x = max(x, screen.left() + 8)
        y = ball_rect.top() - h - 4
        if y < screen.top() + 8:
            y = ball_rect.bottom() + 4
        self.move(x, y)

    def update_session(self, session_id: str, state: dict):
        if state.get("status") == "removed":
            if session_id in self._session_tabs:
                tab = self._session_tabs.pop(session_id)
                self._tabs_layout.removeWidget(tab)
                tab.deleteLater()
            if self._filter_sid == session_id:
                self.set_filter(None)
        else:
            created = session_id not in self._session_tabs
            if created:
                tab = SessionTab(session_id)
                tab.clicked.connect(self._show_detail)
                tab.setVisible(self._filter_sid is None)
                self._session_tabs[session_id] = tab
                idx = self._tabs_layout.count() - 1
                self._tabs_layout.insertWidget(idx, tab)
            tab = self._session_tabs[session_id]
            prev_status = getattr(tab, "_last_status", None)
            tab.update_state(state)
            tab._last_status = state.get("status")
            # 仅状态变化才重排，高频消息时避免每条都全量重排
            if created or tab._last_status != prev_status:
                self._sort_tabs()

        has = len(self._session_tabs) > 0
        self._empty_label.setVisible(not has)

    _STATUS_ORDER = {"waiting_permission": 0, "working": 1, "waiting": 2, "idle": 3}

    def _sort_tabs(self):
        tabs = list(self._session_tabs.items())
        tabs.sort(key=lambda kv: self._STATUS_ORDER.get(
            sessions.get(kv[0], {}).get("status", "idle"), 4))
        for i, (sid, tab) in enumerate(tabs):
            self._tabs_layout.insertWidget(i, tab)

    def _show_detail(self, session_id: str):
        dlg = SessionDetailDialog(session_id, self)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def enterEvent(self, event):
        self._ball._cancel_hide()

    def leaveEvent(self, event):
        self._ball._schedule_hide()


class SlingOverlay(QWidget):
    """弹弓覆盖层：全虚拟桌面透明窗口（鼠标穿透），画拉弓皮筋和飞行中的会话点。
    皮筋会拉出悬浮球窗口范围，必须在独立覆盖层上画；物理循环 16ms 一帧。"""
    GRAVITY = 2400.0      # px/s^2
    DAMPING = 0.62        # 边缘反弹速度保留比例
    FLY_SECONDS = 2.4     # 自由飞行时长，之后飞回悬浮球归位
    DOT_R = 7.0

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._pull = None    # {"anchor": QPointF, "cur": QPointF, "color": QColor}
        self._dots = []      # 飞行中: {sid,pos,vel,color,t,phase,home_fn,done_fn}
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def _ensure_geometry(self):
        self.setGeometry(QApplication.primaryScreen().virtualGeometry())

    def set_pull(self, anchor: QPointF, cur: QPointF, color: QColor):
        self._ensure_geometry()
        self._pull = {"anchor": anchor, "cur": cur, "color": color}
        if not self.isVisible():
            self.show()
        self.update()

    def clear_pull(self):
        self._pull = None
        self.update()

    def add_dot(self, sid: str, pos: QPointF, vel: QPointF, color: QColor,
                home_fn, done_fn):
        self._ensure_geometry()
        self._dots.append({"sid": sid, "pos": pos, "vel": vel, "color": color,
                           "t": 0.0, "phase": "fly", "trail": [],
                           "home_fn": home_fn, "done_fn": done_fn})
        if not self.isVisible():
            self.show()
        if not self._timer.isActive():
            self._timer.start(16)

    def maybe_hide(self):
        if self._pull is None and not self._dots:
            self._timer.stop()
            self.hide()

    def _tick(self):
        dt = 0.016
        geo = self.geometry()
        finished = []
        for d in self._dots:
            if d["phase"] == "fly":
                d["t"] += dt
                d["vel"].setY(d["vel"].y() + self.GRAVITY * dt)
                d["pos"] = d["pos"] + d["vel"] * dt
                r = self.DOT_R
                if d["pos"].x() < geo.left() + r:
                    d["pos"].setX(geo.left() + r)
                    d["vel"].setX(-d["vel"].x() * self.DAMPING)
                elif d["pos"].x() > geo.right() - r:
                    d["pos"].setX(geo.right() - r)
                    d["vel"].setX(-d["vel"].x() * self.DAMPING)
                if d["pos"].y() < geo.top() + r:
                    d["pos"].setY(geo.top() + r)
                    d["vel"].setY(-d["vel"].y() * self.DAMPING)
                elif d["pos"].y() > geo.bottom() - r:
                    d["pos"].setY(geo.bottom() - r)
                    d["vel"].setY(-d["vel"].y() * self.DAMPING)
                    d["vel"].setX(d["vel"].x() * 0.985)  # 贴地摩擦
                if d["t"] >= self.FLY_SECONDS:
                    d["phase"] = "return"
            else:  # return: 缓动飞回悬浮球上的家
                home = d["home_fn"]()
                if home is None:        # 会话已结束，点直接消失
                    finished.append(d)
                    continue
                delta = home - d["pos"]
                if math.hypot(delta.x(), delta.y()) < 5:
                    finished.append(d)
                    continue
                d["pos"] = d["pos"] + delta * 0.22
        for d in self._dots:
            # 拖尾：存副本（bounce 分支会原地 setX/setY 改 pos 对象）
            d["trail"].append(QPointF(d["pos"]))
            if len(d["trail"]) > 7:
                d["trail"].pop(0)
        for d in finished:
            self._dots.remove(d)
            try:
                d["done_fn"](d["sid"])
            except Exception:
                logger.exception("弹弓归位回调失败")
        self.update()
        self.maybe_hide()

    def _draw_dot(self, p: QPainter, pos: QPointF, color: QColor):
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(pos, self.DOT_R, self.DOT_R)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        off = QPointF(self.geometry().topLeft())
        if self._pull:
            a = self._pull["anchor"] - off
            c = self._pull["cur"] - off
            dx, dy = c.x() - a.x(), c.y() - a.y()
            dist = math.hypot(dx, dy) or 1.0
            # 叉臂锚点：垂直于拉伸方向两侧偏移
            px, py = -dy / dist * 8, dx / dist * 8
            p.setPen(QPen(QColor(0, 229, 160, 190), 2.2,
                          Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(a.x() + px, a.y() + py), c)
            p.drawLine(QPointF(a.x() - px, a.y() - py), c)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor(110, 91, 255, 230)))
            p.drawEllipse(QPointF(a.x() + px, a.y() + py), 2.5, 2.5)
            p.drawEllipse(QPointF(a.x() - px, a.y() - py), 2.5, 2.5)
            self._draw_dot(p, c, self._pull["color"])
        for d in self._dots:
            trail = d["trail"]
            n = len(trail)
            for i, tp in enumerate(trail):
                c = QColor(d["color"])
                c.setAlpha(int(60 * (i + 1) / n))
                rr = self.DOT_R * (0.25 + 0.55 * (i + 1) / n)
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(c))
                p.drawEllipse(tp - off, rr, rr)
            self._draw_dot(p, d["pos"] - off, d["color"])


class FloatingBall(QWidget):
    BALL_SIZE = 14
    BALL_GAP = 6
    PADDING = 10
    ICON_SIZE = 16
    ICON_COLOR = QColor(255, 179, 26)  # 明亮橙 #FFB31A（赛博主题，原 Claude 品牌橙 217,119,87）

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)

        self._drag_pos = None
        self._press_global = None
        self._press_element: str | None = None
        self._sling: dict | None = None          # 拉弓中: {sid, anchor, color}
        self._flying_hidden: set[str] = set()    # 在外面飞的点（球上画空位圈）
        self._sling_overlay: SlingOverlay | None = None
        self._blink_state = False
        self._hover_element: str | None = None
        self._icon_angle = 0
        self._spin_timer = QTimer(self)
        self._spin_timer.timeout.connect(self._spin_tick)
        self._update_spin()

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - 80, screen.bottom() - 60)
        self._refresh_size()

        self._panel = SessionPanel(self)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._panel.hide)

        self._blink_timer = QTimer(self)
        self._blink_timer.timeout.connect(self._blink_tick)
        self._blink_timer.start(600)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._drain_queue)
        self._poll_timer.start(50)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._refresh_elapsed)
        self._elapsed_timer.start(1000)

        # Claude Code 崩溃时 SessionEnd 不会触发，定期清理长时间无更新的会话
        self._cleanup_timer = QTimer(self)
        self._cleanup_timer.timeout.connect(self._cleanup_stale_sessions)
        self._cleanup_timer.start(60_000)

        self._hist_flush_timer = QTimer(self)
        self._hist_flush_timer.timeout.connect(_flush_history)
        self._hist_flush_timer.start(30_000)

        bus.session_update.connect(self._on_session_update)
        bus.permission_request.connect(self._on_permission_request)
        bus.notification.connect(self._on_notification)
        bus.system_warning.connect(self._on_system_warning)

    def _cleanup_stale_sessions(self):
        now = time.time()
        for sid in list(sessions.keys()):
            ts = sessions[sid].get("last_update_ts", 0)
            if ts and now - ts > STALE_SESSION_SECONDS:
                logger.info("清理无更新会话: %s", sid[:8])
                ReminderDialog.dismiss_for(sid)
                sessions.pop(sid, None)
                bus.session_update.emit(sid, {"status": "removed"})

    def _refresh_size(self):
        n = max(len(sessions), 1)
        w = (self.PADDING * 2 + self.ICON_SIZE + self.BALL_GAP
             + n * (self.BALL_SIZE + self.BALL_GAP) - self.BALL_GAP)
        h = max(self.ICON_SIZE, self.BALL_SIZE) + self.PADDING * 2
        self.setFixedSize(w, h)

    def _schedule_hide(self):
        self._hide_timer.start(200)

    def _cancel_hide(self):
        self._hide_timer.stop()

    def _refresh_elapsed(self):
        for tab in self._panel._session_tabs.values():
            tab.refresh_elapsed()

    def _drain_queue(self):
        while True:
            try:
                msg = _msg_queue.get_nowait()
            except queue.Empty:
                break
            try:
                _process_msg(msg)
            except Exception:
                logger.exception("处理消息失败: type=%s", msg.get("type"))

    def _blink_tick(self):
        # 没有出错会话且当前不在闪烁半程时跳过重绘
        has_error = any(s.get("status") == "error" for s in sessions.values())
        if not has_error and not self._blink_state:
            return
        self._blink_state = not self._blink_state
        self.update()

    def _spin_tick(self):
        self._icon_angle = (self._icon_angle + 2) % 360
        self.update()

    def _update_spin(self):
        # 空闲不转：没有 working 会话时停掉 40ms 旋转定时器省 CPU
        active = any(s.get("status") == "working" for s in sessions.values())
        if active and not self._spin_timer.isActive():
            self._spin_timer.start(40)
        elif not active and self._spin_timer.isActive():
            self._spin_timer.stop()
            self.update()

    def _dots_x0(self) -> int:
        return self.PADDING + self.ICON_SIZE + self.BALL_GAP

    def _element_at(self, pos) -> str:
        """命中检测：返回 "__icon__"（Claude 图标）或对应 session_id"""
        sids = list(sessions.keys())
        if not sids or pos.x() < self._dots_x0() - self.BALL_GAP / 2:
            return "__icon__"
        step = self.BALL_SIZE + self.BALL_GAP
        idx = int((pos.x() - self._dots_x0() + self.BALL_GAP / 2) // step)
        return sids[min(max(idx, 0), len(sids) - 1)]

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 玻璃胶囊球体：半透明渐变底 + 顶部高光弧 + 浅描边（仿毛玻璃）
        body = QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75)
        radius = body.height() / 2
        grad = QLinearGradient(body.topLeft(), body.bottomLeft())
        grad.setColorAt(0.0, QColor(46, 48, 66, 150))
        grad.setColorAt(1.0, QColor(16, 16, 26, 178))
        painter.setPen(QPen(QColor(255, 255, 255, 52), 1.0))
        painter.setBrush(QBrush(grad))
        painter.drawRoundedRect(body, radius, radius)
        hi = QRectF(body.x() + radius * 0.5, body.y() + 1.8,
                    body.width() - radius, body.height() * 0.40)
        hg = QLinearGradient(hi.topLeft(), hi.bottomLeft())
        hg.setColorAt(0.0, QColor(255, 255, 255, 46))
        hg.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(hg))
        painter.drawRoundedRect(hi, hi.height() / 2, hi.height() / 2)

        # Claude 星芒图标：悬浮显示全部会话
        cy = self.height() / 2
        cx = self.PADDING + self.ICON_SIZE / 2
        painter.setPen(QPen(self.ICON_COLOR, 2.0, Qt.SolidLine, Qt.RoundCap))
        for ang in range(0, 360, 45):
            rad = math.radians(ang + self._icon_angle)
            painter.drawLine(
                QPointF(cx + 2.5 * math.cos(rad), cy + 2.5 * math.sin(rad)),
                QPointF(cx + (self.ICON_SIZE / 2) * math.cos(rad),
                        cy + (self.ICON_SIZE / 2) * math.sin(rad)))

        sids = list(sessions.keys()) if sessions else ["__placeholder__"]
        y = (self.height() - self.BALL_SIZE) // 2
        for i, sid in enumerate(sids):
            x = self._dots_x0() + i * (self.BALL_SIZE + self.BALL_GAP)
            if sid in self._flying_hidden:
                # 点被弹弓打出去了，画个空位圈等它飞回来
                painter.setPen(QPen(QColor(0, 229, 160, 90), 1.2))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(x, y, self.BALL_SIZE, self.BALL_SIZE)
                continue
            state = sessions.get(sid, {})
            status = state.get("status", "idle") if sid != "__placeholder__" else "idle"
            color = STATUS_COLORS.get(status, STATUS_COLORS["idle"])
            if status == "error" and self._blink_state:
                color = QColor(color.red(), color.green(), color.blue(), 80)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(color))
            painter.drawEllipse(x, y, self.BALL_SIZE, self.BALL_SIZE)

    def _apply_hover(self, element: str):
        """悬浮 Claude 图标显示全部会话，悬浮单个点只显示该会话"""
        if element == self._hover_element and self._panel.isVisible():
            return
        self._hover_element = element
        self._panel.set_filter(None if element == "__icon__" else element)
        self._panel.reposition(self.frameGeometry())
        self._panel.show()
        self._panel.raise_()

    def enterEvent(self, event):
        self._cancel_hide()
        if self._sling is None:
            self._apply_hover(self._element_at(event.pos()))

    def leaveEvent(self, event):
        self._hover_element = None
        self._schedule_hide()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            self._press_global = event.globalPos()
            self._press_element = self._element_at(event.pos())

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() == Qt.LeftButton:
            elem = self._press_element
            if elem and elem != "__icon__" and elem in sessions:
                # 会话点不拖窗口：拖出阈值进入弹弓模式
                if (event.globalPos() - self._press_global).manhattanLength() >= 6:
                    self._update_sling(elem, event.globalPos())
            else:
                # 只有 Claude 图标（及空白区）拖动窗口
                self.move(event.globalPos() - self._drag_pos)
        elif not event.buttons():
            self._apply_hover(self._element_at(event.pos()))
            if self._panel.isVisible():
                self._panel.reposition(self.frameGeometry())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            sling = self._sling
            self._sling = None
            is_click = (self._press_global is not None
                        and (event.globalPos() - self._press_global).manhattanLength() < 6)
            self._drag_pos = None
            self._press_global = None
            self._press_element = None
            if sling is not None:
                self._launch_sling(sling, event.globalPos())
                return
            if is_click:
                element = self._element_at(event.pos())
                if element != "__icon__" and element in sessions:
                    self._focus_terminal(element)

    # ---------- 弹弓（拖会话点打着玩，会话本身不受影响） ----------

    def _overlay(self) -> SlingOverlay:
        if self._sling_overlay is None:
            self._sling_overlay = SlingOverlay()
        return self._sling_overlay

    def _dot_center_global(self, sid: str) -> QPointF | None:
        sids = list(sessions.keys())
        if sid not in sids:
            return None
        i = sids.index(sid)
        x = self._dots_x0() + i * (self.BALL_SIZE + self.BALL_GAP) + self.BALL_SIZE / 2
        y = self.height() / 2
        return QPointF(self.mapToGlobal(QPoint(int(x), int(y))))

    def _dot_color(self, sid: str) -> QColor:
        status = sessions.get(sid, {}).get("status", "idle")
        return QColor(STATUS_COLORS.get(status, STATUS_COLORS["idle"]))

    def _update_sling(self, sid: str, gpos):
        if self._sling is None:
            anchor = self._dot_center_global(sid)
            if anchor is None:
                return
            self._sling = {"sid": sid, "anchor": anchor,
                           "color": self._dot_color(sid)}
            self._flying_hidden.add(sid)   # 点已被拉到皮筋上，球上画空位
            self._panel.hide()             # 拉弓时收起面板防遮挡
            self.update()
        self._overlay().set_pull(self._sling["anchor"], QPointF(gpos),
                                 self._sling["color"])

    def _launch_sling(self, sling: dict, gpos):
        ov = self._overlay()
        ov.clear_pull()
        sid = sling["sid"]
        pull = sling["anchor"] - QPointF(gpos)
        dist = math.hypot(pull.x(), pull.y())
        if dist < 15:
            # 拉伸太短视为误拖，直接归位
            self._flying_hidden.discard(sid)
            ov.maybe_hide()
            self.update()
            return
        speed = min(dist * 9.0, 3200.0)
        vel = QPointF(pull.x() / dist * speed, pull.y() / dist * speed)
        ov.add_dot(sid, QPointF(gpos), vel, sling["color"],
                   home_fn=lambda s=sid: self._dot_center_global(s),
                   done_fn=self._sling_done)

    def _sling_done(self, sid: str):
        self._flying_hidden.discard(sid)
        self.update()

    def _focus_terminal(self, sid: str):
        """点击会话点 → 聚焦其终端窗口；仅当确认窗口已关才移除该点"""
        hwnd = int(sessions.get(sid, {}).get("term_hwnd") or 0)
        import ctypes
        user32 = ctypes.windll.user32
        if not hwnd:
            # 未记录 ≠ 已关闭（老会话还没补录），不能误删活会话的点
            QToolTip.showText(QCursor.pos(), "该会话暂未记录终端窗口，下次工具调用后自动补录", self)
            return
        if not user32.IsWindow(hwnd):
            QToolTip.showText(QCursor.pos(), "终端窗口已关闭，移除该会话", self)
            self._remove_session(sid)
            return
        # 存量数据可能记的是 ConPTY 幻影窗，聚焦前再解析一次真窗口
        root = user32.GetAncestor(hwnd, 3)  # GA_ROOTOWNER
        if root and root != hwnd and user32.IsWindowVisible(root):
            hwnd = root
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        # WT 多标签页共用一个窗口句柄，系统没有"聚焦到指定标签页"的接口，
        # 只能聚焦到窗口级；提示目录名让用户自己切标签
        cwd = sessions.get(sid, {}).get("cwd", "")
        proj = cwd.replace("\\", "/").rstrip("/").split("/")[-1] if cwd else sid[:8]
        same = 0
        for s in sessions.values():
            h2 = int(s.get("term_hwnd") or 0)
            if not h2:
                continue
            r2 = user32.GetAncestor(h2, 3)  # GA_ROOTOWNER，幻影窗归并到真窗口
            root2 = r2 if (r2 and user32.IsWindowVisible(r2)) else h2
            if root2 == hwnd:
                same += 1
        tip = f"已聚焦终端 · 会话目录: {proj}"
        if same > 1:
            tip += f"\n该窗口有 {same} 个会话标签，请切到对应标签页"
        QToolTip.showText(QCursor.pos(), tip, self)

    def _remove_session(self, sid: str):
        """移除会话点：内存 + sessions.json 同步删除（终端已关，SessionEnd 不会再来）"""
        ReminderDialog.dismiss_for(sid)
        sessions.pop(sid, None)
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if sid in data:
                data.pop(sid)
                tmp = STATE_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, STATE_FILE)
        except Exception:
            logger.exception("从 sessions.json 移除会话失败")
        bus.session_update.emit(sid, {"status": "removed"})

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background: #1E1E2A; color: #D8D8E8; border: 1px solid rgba(200,200,255,0.15);
                    border-radius: 6px; padding: 4px; font-size: 12px; }
            QMenu::item { padding: 5px 24px; border-radius: 4px; }
            QMenu::item:selected { background: rgba(110,91,255,0.45); }
        """)
        act_history = QAction("查看历史", menu)
        act_quit = QAction("退出 HUD", menu)
        act_history.triggered.connect(self._show_history)
        act_quit.triggered.connect(QApplication.instance().quit)
        menu.addAction(act_history)
        menu.addSeparator()
        menu.addAction(act_quit)
        menu.exec_(event.globalPos())

    def _show_history(self):
        dlg = HistoryDialog(self)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_system_warning(self, message: str):
        QToolTip.showText(self.mapToGlobal(QPoint(0, -34)), f"HUD 警告: {message}", self)

    def _on_session_update(self, session_id: str, state: dict):
        self._panel.update_session(session_id, state)
        self._update_spin()
        self._refresh_size()
        self.update()
        if self._panel.isVisible():
            self._panel.reposition(self.frameGeometry())

    def _reposition_popups(self):
        """权限弹窗与提醒窗锚定悬浮球，从球上方向上堆叠（上方不够则放球下方）"""
        rect = self.frameGeometry()
        # 取球所在屏幕（支持多显示器），球在屏幕间隙时回退主屏
        screen_obj = QApplication.screenAt(rect.center()) or QApplication.primaryScreen()
        screen = screen_obj.availableGeometry()
        popups = (list(PermissionDialog._open_dialogs)
                  + list(ReminderDialog._by_session.values()))
        offset = 0
        for dlg in popups:
            x = max(screen.left() + 8,
                    min(rect.center().x() - dlg.width() // 2,
                        screen.right() - dlg.width() - 8))
            y = rect.top() - dlg.height() - 8 - offset
            if y < screen.top() + 8:
                y = rect.bottom() + 8 + offset
            dlg.move(x, y)
            offset += dlg.height() + 10

    def moveEvent(self, event):
        super().moveEvent(event)
        self._reposition_popups()

    def _on_notification(self, session_id: str, data: dict):
        existing = ReminderDialog._by_session.get(session_id)
        if existing:
            existing.update_message(data.get("message", ""))
            existing.show()
            existing.raise_()
            self._reposition_popups()
            return
        dlg = ReminderDialog(session_id, data, self)
        self._reposition_popups()
        dlg.show()
        dlg.raise_()

    def _on_permission_request(self, request_id: str, data: dict):
        dlg = PermissionDialog(request_id, data, self)
        self._reposition_popups()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()


# 保持向后兼容的别名
def _already_running() -> bool:
    """通过 WS 端口探测是否已有 HUD 实例，避免重复启动产生僵尸悬浮球"""
    try:
        with socket.create_connection(("localhost", WS_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def main():
    if _already_running():
        logger.info("HUD 已在运行（端口 %d 被占用），本实例退出", WS_PORT)
        sys.exit(0)

    # 先从文件恢复已有 session 状态
    persisted = load_persisted_sessions()
    sessions.update(persisted)
    _load_history()

    loop = asyncio.new_event_loop()
    ws_thread = threading.Thread(target=run_ws_server, args=(loop,), daemon=True)
    ws_thread.start()

    udp_thread = threading.Thread(target=run_udp_listener, daemon=True)
    udp_thread.start()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Claude Code HUD")
    app.aboutToQuit.connect(_flush_history)

    hud = FloatingBall()

    # 把恢复的 session 渲染到 UI
    for sid, state in persisted.items():
        bus.session_update.emit(sid, state)

    hud.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
