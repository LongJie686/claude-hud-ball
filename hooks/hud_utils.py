"""notify.py / permission.py 共用工具：HUD 存活探测与拉起、跨进程文件锁"""
from __future__ import annotations  # 兼容 Python 3.9（hook 由系统 python 运行）

import os
import sys
import time
import socket
import subprocess

HUD_WS_PORT = 17890


def hud_alive(timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("localhost", HUD_WS_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def launch_hud() -> None:
    script = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "hud.py"))
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pythonw if os.path.exists(pythonw) else sys.executable
    try:
        subprocess.Popen(
            [exe, script],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
    except Exception:
        pass


def acquire_file_lock(path: str, timeout: float = 2.0):
    """以 O_EXCL 创建 .lock 文件作为跨进程锁，指数退避轮询。
    stale lock（>10s）自动清理。失败返回 (None, lock_path)，调用方降级为无锁操作。"""
    lock_path = path + ".lock"
    deadline = time.time() + timeout
    wait = 0.05
    while time.time() < deadline:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            return fd, lock_path
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 10:
                    os.unlink(lock_path)
                    continue
            except OSError:
                pass
            time.sleep(wait)
            wait = min(wait * 2, 0.4)
    return None, lock_path


def release_file_lock(fd, lock_path: str) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(lock_path)
    except OSError:
        pass
