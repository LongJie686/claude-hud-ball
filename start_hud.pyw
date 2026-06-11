"""
start_hud.pyw - 无窗口启动脚本（.pyw 不弹黑窗口）
用于开机自启，检查HUD是否已运行，未运行则启动。
"""
import subprocess
import socket
import sys
import os

HUD_PORT = 17890
HUD_DIR = os.path.dirname(os.path.abspath(__file__))
HUD_SCRIPT = os.path.join(HUD_DIR, "hud.py")
PYTHON = sys.executable


def is_hud_running() -> bool:
    try:
        with socket.create_connection(("localhost", HUD_PORT), timeout=0.5):
            return True
    except OSError:
        return False


if not is_hud_running():
    subprocess.Popen(
        [PYTHON, HUD_SCRIPT],
        cwd=HUD_DIR,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
