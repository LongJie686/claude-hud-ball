"""切换自动放行开关（/aa 斜杠命令后端）。

用法:
    python toggle_auto_allow.py <session_id>          切换该会话的自动放行
    python toggle_auto_allow.py <session_id> global   切换全局自动放行
    python toggle_auto_allow.py <session_id> status   只查状态不改动

与 HUD 悬浮球共用 config.json；permission.py hook 每次触发实时读取，
HUD 侧有 mtime 监视，球圈/描边会自动跟随变化。输出 ASCII，避免 GBK 控制台乱码。
"""
import json
import os
import sys


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1]:
        print("ERROR: missing session_id")
        return 1
    sid = sys.argv[1]
    action = sys.argv[2].lower() if len(sys.argv) > 2 else ""

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception:
        cfg = {}
    sess = cfg.get("auto_allow_sessions") or {}

    if action == "status":
        print("global=%s session=%s (sid=%s)" % (
            "ON" if cfg.get("auto_allow") else "OFF",
            "ON" if sess.get(sid) else "OFF", sid[:8]))
        return 0

    if action == "global":
        cfg["auto_allow"] = not cfg.get("auto_allow", False)
        state = "ON" if cfg["auto_allow"] else "OFF"
        scope = "GLOBAL"
    else:
        on = not sess.get(sid, False)
        if on:
            sess[sid] = True
        else:
            sess.pop(sid, None)
        cfg["auto_allow_sessions"] = sess
        state = "ON" if on else "OFF"
        scope = "SESSION " + sid[:8]

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    print("AUTO_ALLOW %s -> %s" % (scope, state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
