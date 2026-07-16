"""
Claude Code HUD Hook 脚本
PreToolUse / PostToolUse / Stop / SessionStart / SessionEnd
"""
from __future__ import annotations  # 兼容 Python 3.9（hook 由系统 python 运行）

import sys
import json
import os
from datetime import datetime
from typing import Any

from hud_utils import hud_alive, launch_hud, acquire_file_lock, release_file_lock

HUD_WS_PORT = 17890
STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "sessions.json")
STATE_FILE = os.path.normpath(STATE_FILE)
LOG_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "logs"))


def _setup_logger():
    import logging
    from logging.handlers import RotatingFileHandler
    os.makedirs(LOG_DIR, exist_ok=True)
    lg = logging.getLogger("hud.notify")
    lg.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "hooks.log"),
        maxBytes=512 * 1024, backupCount=1, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    lg.addHandler(handler)
    return lg


logger = _setup_logger()


# ---------- 状态文件操作 ----------

def _read_state() -> dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(state: dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def update_session(session_id: str, data: dict[str, Any]) -> None:
    import time
    # 多会话并发 hook 同时读改写 sessions.json 会互相覆盖，加文件锁
    fd, lock = acquire_file_lock(STATE_FILE)
    try:
        state = _read_state()
        existing = state.get(session_id, {})
        existing.update(data)
        existing["last_update"] = datetime.now().strftime("%H:%M:%S")
        existing["last_update_ts"] = time.time()
        state[session_id] = existing
        _write_state(state)
    finally:
        release_file_lock(fd, lock)


def remove_session(session_id: str) -> None:
    fd, lock = acquire_file_lock(STATE_FILE)
    try:
        state = _read_state()
        state.pop(session_id, None)
        _write_state(state)
    finally:
        release_file_lock(fd, lock)


# ---------- HUD 通信 ----------
# 状态上报走 UDP（fire-and-forget，不 import websockets/asyncio，hook 更轻）
# HUD 存活探测复用 WS 的 TCP 端口

HUD_UDP_PORT = 17891


def _slim(obj: Any, limit: int = 2000) -> Any:
    """截断超长字段，防止 UDP 报文超限（仅影响状态展示，权限弹窗仍走 WS 全量）"""
    if isinstance(obj, str) and len(obj) > limit:
        return obj[:limit] + "…(截断)"
    if isinstance(obj, dict):
        return {k: _slim(v, limit) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_slim(v, limit) for v in obj[:20]]
    return obj


def send_msg(payload: dict[str, Any]) -> None:
    import socket
    if not hud_alive():
        # 不等 HUD 起来：事件在发 UDP 前已写入 sessions.json，HUD 启动时会从文件恢复，
        # 丢掉这条 UDP 不影响最终状态（原先 sleep(1.5) 拖慢每次 hook）
        launch_hud()
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(data, ("127.0.0.1", HUD_UDP_PORT))
    except Exception:
        logger.warning("UDP 上报失败: type=%s", payload.get("type"))


# ---------- 终端窗口定位（点击悬浮球的点时聚焦对应终端） ----------

def _find_terminal_hwnd() -> "tuple[int, int]":
    """沿父进程链向上找拥有可见顶层窗口的祖先（终端宿主），返回 (hwnd, pid)。
    hook 进程链一般是: python(hook) → node(claude) → shell → 终端。找不到返回 (0, 0)。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    TH32CS_SNAPPROCESS = 0x2
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return 0, 0
    ppid_map: dict[int, int] = {}
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    if kernel32.Process32First(snap, ctypes.byref(entry)):
        while True:
            ppid_map[entry.th32ProcessID] = entry.th32ParentProcessID
            if not kernel32.Process32Next(snap, ctypes.byref(entry)):
                break
    kernel32.CloseHandle(snap)

    ancestors: list[int] = []
    pid = os.getpid()
    for _ in range(12):
        pid = ppid_map.get(pid, 0)
        if not pid:
            break
        ancestors.append(pid)

    pid_windows: dict[int, int] = {}
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        pid = owner.value
        # 同进程多窗口时优先取有标题的（无标题多为辅助窗）
        if pid not in pid_windows:
            pid_windows[pid] = hwnd
        elif (user32.GetWindowTextLengthW(pid_windows[pid]) == 0
                and user32.GetWindowTextLengthW(hwnd) > 0):
            pid_windows[pid] = hwnd
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)

    for pid in ancestors:
        hwnd = pid_windows.get(pid)
        if hwnd:
            # Win11 默认终端委托：cmd 名下挂的是 ConPTY 幻影窗(PseudoConsoleWindow)，
            # 其 owner 才是真正的 Windows Terminal 窗口，聚焦幻影窗无效
            root = user32.GetAncestor(hwnd, 3)  # GA_ROOTOWNER
            if root and root != hwnd and user32.IsWindowVisible(root):
                hwnd = root
            return hwnd, pid
    return 0, 0


def _find_cc_pid() -> "tuple[int, str]":
    """沿父进程链找 Claude Code 主进程（node/claude/bun/deno）。
    终端标签被直接关闭时 SessionEnd 不会触发，HUD 靠定期检查该 pid
    是否存活来及时摘掉小球上的点"""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
        if snap == -1:
            return 0, ""
        ppid: dict = {}
        names: dict = {}
        e = PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if kernel32.Process32First(snap, ctypes.byref(e)):
            while True:
                ppid[e.th32ProcessID] = e.th32ParentProcessID
                names[e.th32ProcessID] = e.szExeFile.decode(errors="replace").lower()
                if not kernel32.Process32Next(snap, ctypes.byref(e)):
                    break
        kernel32.CloseHandle(snap)
        pid = os.getpid()
        for _ in range(12):
            pid = ppid.get(pid, 0)
            if not pid:
                break
            if names.get(pid, "") in ("node.exe", "claude.exe", "bun.exe", "deno.exe"):
                return pid, names[pid]
        return 0, ""
    except Exception:
        return 0, ""


def _foreground_hwnd() -> int:
    """取当前前台窗口。UserPromptSubmit 时用户刚在该终端敲回车，
    前台窗口即会话所在终端/IDE——进程父链断裂（PyCharm 等经
    pty 引导进程拉起 shell）时的最可靠兜底"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if hwnd and user32.IsWindowVisible(hwnd) \
                and user32.GetWindowTextLengthW(hwnd) > 0:
            return hwnd
    except Exception:
        pass
    return 0


# ---------- 主逻辑 ----------

def main() -> None:
    # Claude Code 以 UTF-8 传输，Windows 下 sys.stdin 默认 GBK 会破坏含中文的 JSON
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    if not raw.strip():
        sys.exit(0)

    try:
        event = json.loads(raw)
    except Exception:
        sys.exit(0)

    hook_event = event.get("hook_event_name", "")
    session_id = event.get("session_id", "unknown")
    cwd = event.get("cwd") or os.getcwd()

    if hook_event == "PreToolUse":
        tool_name = event.get("tool_name", "")
        tool_input = _slim(event.get("tool_input", {}))
        data = {
            "status": "working",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "cwd": cwd,
        }
        # 旧会话补录终端窗口（SessionStart 早于该功能或当时捕获失败）
        state = _read_state().get(session_id, {})
        if not state.get("term_hwnd"):
            try:
                hwnd, tpid = _find_terminal_hwnd()
            except Exception:
                hwnd, tpid = 0, 0
            if hwnd:
                data["term_hwnd"] = hwnd
                data["term_pid"] = tpid
        if not state.get("cc_pid"):
            cc_pid, cc_exe = _find_cc_pid()
            if cc_pid:
                data["cc_pid"] = cc_pid
                data["cc_exe"] = cc_exe
        update_session(session_id, data)
        send_msg({"type": "pre_tool", "session_id": session_id,
                  "tool_name": tool_name, "tool_input": tool_input, "cwd": cwd,
                  "term_hwnd": data.get("term_hwnd", 0),
                  "cc_pid": data.get("cc_pid", 0),
                  "cc_exe": data.get("cc_exe", "")})

    elif hook_event == "PostToolUse":
        tool_name = event.get("tool_name", "")
        resp = event.get("tool_response")
        is_error = False
        if isinstance(resp, dict):
            is_error = bool(resp.get("is_error") or resp.get("isError")
                            or resp.get("error"))
        update_session(session_id, {
            "status": "error" if is_error else "waiting",
            "tool_name": tool_name,
        })
        send_msg({"type": "post_tool", "session_id": session_id,
                  "tool_name": tool_name, "is_error": is_error})

    elif hook_event == "Stop":
        update_session(session_id, {"status": "idle", "tool_name": "", "tool_input": {}})
        send_msg({"type": "stop", "session_id": session_id})

    elif hook_event == "SessionStart":
        try:
            term_hwnd, term_pid = _find_terminal_hwnd()
        except Exception:
            term_hwnd, term_pid = 0, 0
        if not term_hwnd:
            # 刚敲 claude 启动，前台窗口即所在终端/IDE
            term_hwnd, term_pid = _foreground_hwnd(), 0
        cc_pid, cc_exe = _find_cc_pid()
        update_session(session_id, {"status": "waiting", "cwd": cwd, "tool_name": "",
                                    "term_hwnd": term_hwnd, "term_pid": term_pid,
                                    "cc_pid": cc_pid, "cc_exe": cc_exe})
        send_msg({"type": "session_start", "session_id": session_id, "cwd": cwd,
                  "term_hwnd": term_hwnd, "term_pid": term_pid,
                  "cc_pid": cc_pid, "cc_exe": cc_exe})

    elif hook_event == "SessionEnd":
        remove_session(session_id)
        send_msg({"type": "session_end", "session_id": session_id})

    elif hook_event == "UserPromptSubmit":
        # 敲回车瞬间：①补录终端窗口（祖先链找不到用前台窗口兜底）；
        # ②通知 HUD 学习激活标签（此刻用户正盯着该会话的标签页）
        hwnd = _read_state().get(session_id, {}).get("term_hwnd", 0)
        if not hwnd:
            try:
                hwnd, tpid = _find_terminal_hwnd()
            except Exception:
                hwnd, tpid = 0, 0
            if not hwnd:
                hwnd, tpid = _foreground_hwnd(), 0
            if hwnd:
                update_session(session_id, {"term_hwnd": hwnd,
                                            "term_pid": tpid, "cwd": cwd})
        send_msg({"type": "prompt_submit", "session_id": session_id,
                  "term_hwnd": hwnd or 0})

    elif hook_event == "Notification":
        # 原生确认提示出现（敏感文件/MCP/转终端/空闲等待），转发给 HUD 弹提醒窗
        send_msg({"type": "notification", "session_id": session_id,
                  "message": event.get("message", ""), "cwd": cwd})

    sys.exit(0)


if __name__ == "__main__":
    main()
