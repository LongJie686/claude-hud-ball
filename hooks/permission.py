"""
Claude Code HUD - 权限确认 Hook (PreToolUse)

复刻 Claude Code 原生权限决策：
  优先级：permissions.deny > allow > ask > 内置安全白名单 > 工具默认
  - allow 命中 → 直接放行
  - deny  命中 → 直接拒绝
  - ask   命中 / 默认需要确认 → 转 HUD 弹窗
  - 内置白名单（git status / ls / cat 等无副作用命令）→ 放行

用户在 HUD 点「永久允许」后，规则自动写入 settings.json 的 permissions.allow。
HUD 不可用时不介入（exit 0 + 空 stdout），让 Claude Code 自己走原生确认。
"""
from __future__ import annotations  # 兼容 Python 3.9（hook 由系统 python 运行）

import sys
import json
import os
import re
import shlex
import asyncio
import uuid
import fnmatch
import time
import platform

from hud_utils import launch_hud, acquire_file_lock, release_file_lock

HUD_WS_PORT = 17890
TIMEOUT_SECONDS = 60
# hook 等 HUD 响应的硬上限。须小于 settings.json 里本 hook 的 timeout(600s)。
# 弹窗悬停会暂停 HUD 倒计时（用户在场慢慢看），不能让 hook 先死掉丢弃点击；
# 无人理会时 HUD 自己 60s 就回 fallback，不会真等这么久
WAIT_RESPONSE_SECONDS = 590
LAUNCH_WAIT_SECONDS = 1.5
IS_WINDOWS = platform.system() == "Windows"

SETTINGS_PATH = os.path.normpath(
    os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
)
LOG_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
)
CONFIG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
)


def auto_allow_enabled(session_id: str = "") -> bool:
    """HUD「自动放行模式」开关，config.json 由悬浮球右键菜单写入。
    全局开关（auto_allow）或本会话开关（auto_allow_sessions[sid]）任一开启即免确认；
    除 deny 规则外全部放行；文件缺失/损坏一律视为关闭。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return False
    if cfg.get("auto_allow", False):
        return True
    if session_id:
        return bool((cfg.get("auto_allow_sessions") or {}).get(session_id, False))
    return False


def _setup_logger():
    import logging
    from logging.handlers import RotatingFileHandler
    os.makedirs(LOG_DIR, exist_ok=True)
    lg = logging.getLogger("hud.permission")
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

# 内置 Bash 安全前缀：无副作用的只读 / 查询类命令
# 注意不要加入可写盘或执行任意代码的命令：
#   find(-delete/-exec) awk(system()) sort(-o) npm run/test(任意脚本) git branch(-D 删分支)
SAFE_BASH_PREFIXES = [
    # git 只读
    "git status", "git log", "git diff", "git show", "git branch --list",
    "git branch -a", "git branch -v", "git branch -r",
    "git remote -v", "git remote show", "git config --get",
    "git rev-parse", "git symbolic-ref",
    "git ls-files", "git tag --list", "git tag -l", "git stash list", "git blame",
    # 文件系统只读
    "ls", "dir", "pwd", "cd", "echo",
    "cat", "head", "tail", "less", "more", "type",
    "grep", "which", "where", "tree",
    "wc", "uniq", "stat", "file",
    # 版本查询
    "python --version", "python -V", "python3 --version", "python3 -V",
    "node --version", "node -v", "npm --version", "npm -v",
    "go version", "go env", "java -version", "rustc --version",
    # 包管理只读
    "npm list", "npm ls", "npm view", "npm info",
    "pip list", "pip show", "pip freeze",
    "pnpm list", "pnpm ls",
    "yarn list",
    # 进程 / 系统只读
    "ps", "top", "df", "du", "uname", "hostname", "whoami", "id",
    "tasklist", "ipconfig", "ifconfig", "netstat",
    # docker / k8s 只读
    "docker ps", "docker images", "docker logs", "docker inspect",
    "kubectl get", "kubectl describe", "kubectl logs",
]

# 仅整条命令精确匹配才放行（作为前缀不安全，如 git branch -D / git tag -d）
SAFE_BASH_EXACT = {
    "git branch", "git remote", "git tag", "git stash list",
}

DEFAULT_ALLOW_TOOLS = {
    "Read", "Grep", "Glob", "LS", "NotebookRead",
    "WebFetch", "WebSearch", "TodoWrite", "Task",
}
DEFAULT_ASK_TOOLS = {
    "Bash", "Edit", "Write", "MultiEdit", "NotebookEdit",
}


# 文件锁实现移至 hud_utils.acquire_file_lock（指数退避），与 notify.py 共用

# ---------- settings.json 读写 ----------

def _read_perms_file(path: str) -> dict[str, list[str]]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        perm = data.get("permissions", {}) or {}
        return {k: list(perm.get(k, []) or []) for k in ("allow", "deny", "ask")}
    except FileNotFoundError:
        return {"allow": [], "deny": [], "ask": []}
    except Exception:
        logger.exception("读取 permissions 失败: %s", path)
        return {"allow": [], "deny": [], "ask": []}


def load_permissions(cwd: str = "") -> dict[str, list[str]]:
    """合并 全局 + 项目级 + 项目本地 三层 permissions（与 Claude Code 原生一致）"""
    paths = [SETTINGS_PATH]
    if cwd:
        paths.append(os.path.join(cwd, ".claude", "settings.json"))
        paths.append(os.path.join(cwd, ".claude", "settings.local.json"))
    merged: dict[str, list[str]] = {"allow": [], "deny": [], "ask": []}
    for p in paths:
        part = _read_perms_file(p)
        for k in merged:
            merged[k].extend(part[k])
    return merged


def append_allow_rule(rule: str) -> None:
    """加锁后追加 rule 到 permissions.allow，避免多会话并发覆盖"""
    if not _is_persistable_rule(rule):
        logger.warning("拒绝写入不可持久化规则（含换行/格式非法）: %r", (rule or "")[:120])
        return
    fd, lock_path = acquire_file_lock(SETTINGS_PATH)
    if fd is None:
        return
    try:
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        perm = data.setdefault("permissions", {})
        allow = perm.setdefault("allow", [])
        if rule in allow:
            return
        allow.append(rule)
        tmp = f"{SETTINGS_PATH}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SETTINGS_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    finally:
        release_file_lock(fd, lock_path)


# ---------- 规则匹配 ----------

_RULE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\((.*)\))?$")


def parse_rule(rule: str) -> tuple[str, str] | None:
    m = _RULE_RE.match(rule.strip())
    if not m:
        return None
    return m.group(1), (m.group(2) or "").strip()


def _is_persistable_rule(rule: str) -> bool:
    """规则能否安全写入 settings.json。

    含换行的规则会让 Claude Code /doctor 报「Empty parentheses」非法规则，
    而本模块 parse_rule 的 _RULE_RE 用 (.*)（. 不匹配换行）同样命中不了它，
    等于写出一条两边都用不了的垃圾规则。故只持久化 parse_rule 能解析的单行规则。
    """
    if not rule or "\n" in rule or "\r" in rule:
        return False
    if rule.strip().endswith("()"):   # /doctor: "Empty parentheses" 非法
        return False
    return parse_rule(rule) is not None


def match_rule(rule: str, tool_name: str, tool_input: dict) -> bool:
    parsed = parse_rule(rule)
    if not parsed:
        return False
    rule_tool, pattern = parsed
    if rule_tool != tool_name:
        return False
    if not pattern:
        return True
    if tool_name == "Bash":
        cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        return _match_bash_pattern(pattern, cmd)
    if tool_name in ("Read", "Edit", "Write", "MultiEdit", "NotebookRead", "NotebookEdit"):
        path = ""
        if isinstance(tool_input, dict):
            path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return _match_path_pattern(pattern, path)
    return False


def _match_bash_pattern(pattern: str, cmd: str) -> bool:
    cmd = cmd.strip()
    pattern = pattern.strip()
    if pattern.endswith(":*"):
        prefix = pattern[:-2].strip()
        if not prefix:
            return True
        return cmd == prefix or cmd.startswith(prefix + " ") or cmd.startswith(prefix + "\t")
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatchcase(cmd, pattern)
    return cmd == pattern


def _match_path_pattern(pattern: str, path: str) -> bool:
    if not path:
        return False
    p = path.replace("\\", "/")
    pat = pattern.replace("\\", "/")
    if IS_WINDOWS:
        # Windows 路径不区分大小写
        p = p.lower()
        pat = pat.lower()
    return fnmatch.fnmatchcase(p, pat)


# ---------- Bash 解析 ----------

def has_dangerous_shell_structure(cmd: str) -> bool:
    """
    检测命令中可在 shell 展开时绕过白名单的结构：
      $(...)  反引号 `...`  <(...)  >(...)  重定向 > <
    bash 引号规则：
      - 单引号内全部 literal
      - 双引号内 $( 和 ` 仍展开（其他字面）
      - 非引号内全部生效
    """
    in_single = False
    in_double = False
    i = 0
    n = len(cmd)
    while i < n:
        c = cmd[i]
        # 反斜杠转义下一个字符
        if c == "\\" and not in_single and i + 1 < n:
            i += 2
            continue
        if c == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if c == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if in_single:
            i += 1
            continue
        # 双引号内 $( 和 ` 仍展开
        two = cmd[i:i+2]
        if two == "$(":
            return True
        if c == "`":
            return True
        # 进程替换 / 重定向 只在非引号内生效
        if not in_double:
            if two in ("<(", ">("):
                return True
            if c == "<":
                return True
            if c == ">":
                j = i + 1
                if j < n and cmd[j] == ">":      # >> 追加写同样按目标判断
                    j += 1
                if j < n and cmd[j] == "&":
                    if cmd[j+1:j+2].isdigit():   # N>&M 文件描述符复制（2>&1），无害
                        i = j + 2
                        continue
                    j += 1                        # >&file 整体重定向，按目标判断
                while j < n and cmd[j] in " \t":
                    j += 1
                target = ""
                while j < n and cmd[j] not in " \t;|&<>'\"":
                    target += cmd[j]
                    j += 1
                if target.lower() in ("/dev/null", "nul"):  # 丢弃输出，无害
                    i = j
                    continue
                return True
        i += 1
    return False


def split_bash_subcommands(cmd: str) -> list[str]:
    """按 ; && || | & 拆分，跳过引号内的控制符。&& / || 优先于单 & / |。"""
    parts: list[str] = []
    buf = ""
    in_single = False
    in_double = False
    i = 0
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if c == "'" and not in_double:
            in_single = not in_single
            buf += c
            i += 1
            continue
        if c == '"' and not in_single:
            in_double = not in_double
            buf += c
            i += 1
            continue
        if not (in_single or in_double):
            two = cmd[i:i+2]
            if two in ("&&", "||"):
                parts.append(buf)
                buf = ""
                i += 2
                continue
            # >& / &> 是重定向（2>&1、&>/dev/null），不是命令分隔
            if c == "&" and (cmd[i+1:i+2] == ">" or cmd[i-1:i] == ">"):
                buf += c
                i += 1
                continue
            if c in ("|", ";", "&", "\n"):
                parts.append(buf)
                buf = ""
                i += 1
                continue
        buf += c
        i += 1
    parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def _is_safe_subcommand(sub: str) -> bool:
    s = sub.strip().lower()
    if not s:
        return False
    if s in (e.lower() for e in SAFE_BASH_EXACT):
        return True
    for prefix in SAFE_BASH_PREFIXES:
        p = prefix.lower()
        if s == p or s.startswith(p + " "):
            return True
    return False


# ---------- 决策 ----------

def _bash_rule_patterns(rules: "list[str]") -> "list[str]":
    """提取 Bash 规则的 pattern；裸 Bash 规则（匹配一切）用 "" 表示"""
    pats = []
    for r in rules:
        parsed = parse_rule(r)
        if parsed and parsed[0] == "Bash":
            pats.append(parsed[1])
    return pats


def _decide_bash(cmd: str, perms: dict[str, list[str]]) -> str:
    """段级决策（对齐原生语义）：组合命令拆段后逐段评估。
    deny 整条或任一段命中即拒；allow 须每段被规则或内置白名单覆盖，
    防止 Bash(sleep:*) 这类前缀规则因首段匹配放行整条 `sleep 1; rm -rf /`"""
    cmd = (cmd or "").strip()
    if not cmd:
        return "ask"
    subs = split_bash_subcommands(cmd) or [cmd]

    deny_pats = _bash_rule_patterns(perms.get("deny", []))
    for p in deny_pats:
        if not p or _match_bash_pattern(p, cmd) \
                or any(_match_bash_pattern(p, s) for s in subs):
            return "deny"

    allow_pats = _bash_rule_patterns(perms.get("allow", []))
    if "" in allow_pats:
        return "allow"
    # 精确规则（无 :* 无通配）= 用户批准过的完整命令，整条一致直接放行
    for p in allow_pats:
        if (p and not p.endswith(":*")
                and not any(ch in p for ch in "*?[")
                and _match_bash_pattern(p, cmd)):
            return "allow"
    # 命令替换/反引号/写文件重定向可绕过段级判定，必须人工确认
    if has_dangerous_shell_structure(cmd):
        return "ask"
    if all(_is_safe_subcommand(s)
           or any(p and _match_bash_pattern(p, s) for p in allow_pats)
           for s in subs):
        return "allow"
    return "ask"


def decide(tool_name: str, tool_input: dict, perms: dict[str, list[str]]) -> str:
    if not isinstance(tool_input, dict):
        tool_input = {}

    if tool_name == "Bash":
        return _decide_bash(tool_input.get("command", ""), perms)

    for rule in perms.get("deny", []):
        if match_rule(rule, tool_name, tool_input):
            return "deny"
    for rule in perms.get("allow", []):
        if match_rule(rule, tool_name, tool_input):
            return "allow"
    for rule in perms.get("ask", []):
        if match_rule(rule, tool_name, tool_input):
            return "ask"

    if tool_name in DEFAULT_ALLOW_TOOLS:
        return "allow"
    if tool_name in DEFAULT_ASK_TOOLS:
        return "ask"
    return "ask"


# ---------- HUD 通信 ----------
# HUD 拉起逻辑移至 hud_utils.launch_hud，与 notify.py 共用

async def _wait_response(ws, req_id: str) -> tuple[bool, bool, bool]:
    """循环收消息直到拿到对应 req_id 的 permission_response"""
    while True:
        raw = await ws.recv()
        try:
            resp = json.loads(raw)
        except Exception:
            continue
        if (resp.get("type") == "permission_response"
                and resp.get("request_id") == req_id):
            return (bool(resp.get("approved", False)),
                    bool(resp.get("always_allow", False)),
                    bool(resp.get("fallback", False)))


async def _try_ask_once(event: dict, req_id: str) -> tuple[bool, bool, bool] | None:
    """单次尝试：连接 HUD → 发请求 → 等响应。返回 None 表示连接失败。"""
    import websockets

    uri = f"ws://localhost:{HUD_WS_PORT}"
    try:
        async with websockets.connect(uri, open_timeout=3) as ws:
            await ws.send(json.dumps({
                "type":            "permission_request",
                "request_id":      req_id,
                "session_id":      event.get("session_id", "unknown"),
                "tool_name":       event.get("tool_name", ""),
                "tool_input":      event.get("tool_input", {}),
                "cwd":             event.get("cwd") or os.getcwd(),
                "timeout_seconds": TIMEOUT_SECONDS,
                # 「永久允许」将写入的规则预览；空串 = 无法生成规则、仅放行本次
                "allow_rule":      build_allow_rule(
                    event.get("tool_name", ""),
                    event.get("tool_input", {})) or "",
            }))
            try:
                return await asyncio.wait_for(
                    _wait_response(ws, req_id),
                    timeout=WAIT_RESPONSE_SECONDS,
                )
            except asyncio.TimeoutError:
                # HUD 弹窗自身会在超时后回 fallback；这里是兜底
                return (False, False, True)
    except Exception:
        return None


async def ask_permission(event: dict) -> tuple[bool, bool, bool] | None:
    """
    返回:
      (approved, always_allow, fallback) — HUD 给出了响应
      None                              — HUD 不可用（已尝试启动并重试）
    """
    req_id = str(uuid.uuid4())
    result = await _try_ask_once(event, req_id)
    if result is not None:
        return result
    # 尝试启动 HUD 再试一次
    launch_hud()
    await asyncio.sleep(LAUNCH_WAIT_SECONDS)
    return await _try_ask_once(event, req_id)


# ---------- 永久允许：生成 allow 规则 ----------

def _safe_shlex_split(cmd: str) -> list[str]:
    """Windows 用 posix=False 避免反斜杠被吃掉；失败时降级到普通 split"""
    try:
        return shlex.split(cmd, posix=not IS_WINDOWS)
    except ValueError:
        try:
            return shlex.split(cmd, posix=IS_WINDOWS)
        except ValueError:
            return cmd.split()


# 这些命令破坏性强，"永久允许"只生成精确规则，绝不生成 head:* 宽规则。
# 解释器（python/powershell 等）不在列：2026-07-10 用户明确选择便利优先、
# 宽规则直接放行；弹窗有"将写入规则"预览兜底，点之前知情即可
DANGEROUS_BASH_HEADS = {
    "rm", "del", "rmdir", "rd", "dd", "mkfs", "format", "shred", "diskpart",
    "shutdown", "reboot", "taskkill", "mv", "move", "git push", "git reset",
}

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _extract_bash_head(cmd: str) -> tuple[str, list[str]] | None:
    """提取命令头（含二级子命令，如 'git add'、'opencli browser'）。
    跳过前导 VAR=value 环境变量（曾产出 Bash(MSYS_NO_PATHCONV=1 docker:*) 垃圾规则）。
    返回 (head, tokens)；空命令返回 None。"""
    tokens = _safe_shlex_split(cmd)
    while tokens and _ENV_ASSIGN_RE.match(tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return None
    head = tokens[0]
    if len(tokens) >= 2:
        t1 = tokens[1]
        # 第二个 token 必须是简单子命令名才扩展（避免把文件路径写进规则）
        if (t1 and not t1.startswith("-") and " " not in t1
                and "/" not in t1 and "\\" not in t1
                and not any(ch in t1 for ch in ";|&<>*?[")):
            head = f"{tokens[0]} {t1}"
    return head, tokens


def _head_is_dangerous(head: str, tokens: list[str]) -> bool:
    """head 本身、首 token、及去路径/去 .exe 后的程序名任一命中即视为危险"""
    if head.lower() in DANGEROUS_BASH_HEADS:
        return True
    t0 = tokens[0].lower()
    if t0 in DANGEROUS_BASH_HEADS:
        return True
    base = os.path.basename(t0.replace("\\", "/"))
    if base.endswith(".exe"):
        base = base[:-4]
    return base in DANGEROUS_BASH_HEADS


def build_allow_rule(tool_name: str, tool_input: dict) -> str | None:
    if not isinstance(tool_input, dict):
        return tool_name

    if tool_name == "Bash":
        cmd = (tool_input.get("command", "") or "").strip()
        if not cmd:
            return tool_name
        subs = split_bash_subcommands(cmd)
        if len(subs) > 1:
            # 组合命令：忽略内置安全段（grep/head/echo 等管道消费者）后，
            # 若其余段头部一致且不危险 → 生成公共前缀规则（如 Bash(opencli browser:*)），
            # 这样 `opencli ... && opencli ...` 点一次永久允许即可覆盖后续同类命令。
            # 头部不一致 → 退回整条精确规则；含换行的精确规则无法持久化 → None（放行不落盘）
            heads: dict[str, str] = {}   # lower -> 原始大小写（首见）
            dangerous = False
            # heredoc 正文/重定向目标不是命令，逐段提头会产出 Bash(EOF:*) 类垃圾规则；
            # 且 _decide_bash 对危险结构命令一律 ask，前缀规则也用不上 → 不做前缀提取
            if has_dangerous_shell_structure(cmd):
                dangerous = True
            for s in subs:
                if _is_safe_subcommand(s):
                    continue
                parsed = _extract_bash_head(s)
                if not parsed:
                    continue
                h, toks = parsed
                heads.setdefault(h.lower(), h)
                if _head_is_dangerous(h, toks):
                    dangerous = True
            if len(heads) == 1 and not dangerous:
                rule = f"Bash({next(iter(heads.values()))}:*)"
                if _is_persistable_rule(rule):
                    return rule
            rule = f"Bash({cmd})"
            return rule if _is_persistable_rule(rule) else None
        parsed = _extract_bash_head(cmd)
        if not parsed:
            return tool_name
        head, tokens = parsed
        if _head_is_dangerous(head, tokens):
            rule = f"Bash({cmd})"
            return rule if _is_persistable_rule(rule) else None
        return f"Bash({head}:*)"

    if tool_name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "NotebookRead"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if path:
            d = os.path.dirname(os.path.abspath(path)).replace("\\", "/")
            if d:
                return f"{tool_name}({d}/*)"
        return tool_name

    return tool_name


# ---------- 主逻辑 ----------

_SENSITIVE_ROOT = os.path.normcase(os.path.normpath(os.path.expanduser("~/.claude")))


def _is_sensitive_target(tool_name: str, tool_input: dict) -> bool:
    """目标是否会触发 Claude Code 内置敏感文件保护（hook 无法替代的原生确认）"""
    if tool_name == "Bash":
        # 实测：rm "C:/Users/.../.claude/..." 在 HUD 批准(hook allow)后原生仍强制
        # 确认；而相对路径不触发 → 内置保护按命令串里的 ~/.claude 路径判定。
        # 同样直接转原生，避免 HUD 白弹一次造成双重确认
        cmd = (tool_input.get("command", "") or "").replace("\\", "/").lower()
        sens = _SENSITIVE_ROOT.replace("\\", "/").lower()   # c:/users/xxx/.claude
        variants = [sens, "~/.claude"]
        if len(sens) > 2 and sens[1] == ":":
            variants.append("/" + sens[0] + sens[2:])       # MSYS: /c/users/xxx/.claude
        return any(v in cmd for v in variants)
    if tool_name not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return False
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not path:
        return False
    norm = os.path.normcase(os.path.normpath(path))
    if norm.startswith(_SENSITIVE_ROOT + os.sep) or norm == _SENSITIVE_ROOT:
        return True
    return os.path.basename(norm).startswith(".env")


def emit_decision(decision: str, reason: str = "") -> None:
    """按 Claude Code 官方 PreToolUse hook 协议输出决策"""
    out: dict = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        out["hookSpecificOutput"]["permissionDecisionReason"] = reason
    # ensure_ascii=True: Windows 下 stdout 默认 GBK，中文直出会被 Claude Code
    # 按 UTF-8 解码失败导致整个决策被丢弃（弹窗确认了仍弹原生确认）
    print(json.dumps(out, ensure_ascii=True))


def main() -> None:
    # Claude Code 以 UTF-8 传输，Windows 下 sys.stdin 默认 GBK 会破坏含中文的 JSON
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    if not raw.strip():
        sys.exit(0)

    try:
        event = json.loads(raw)
    except Exception:
        logger.exception("解析 hook 输入失败")
        sys.exit(0)

    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})
    cwd = event.get("cwd") or os.getcwd()
    mode = event.get("permission_mode", "default")

    # 原生不会确认的模式下 HUD 不拦截，保持"HUD 不比原生更吵"
    if mode == "bypassPermissions":
        sys.exit(0)
    if mode == "acceptEdits" and tool_name in (
            "Edit", "Write", "MultiEdit", "NotebookEdit"):
        sys.exit(0)

    perms = load_permissions(cwd)
    decision = decide(tool_name, tool_input, perms)

    if decision == "allow":
        emit_decision("allow", "命中 permissions.allow / 内置安全白名单")
        sys.exit(0)

    if decision == "deny":
        logger.info("deny 规则拒绝: %s %s", tool_name,
                     str(tool_input)[:200])
        emit_decision("deny", "被 settings.json permissions.deny 规则拒绝")
        sys.exit(0)

    # HUD「自动放行模式」：全局或本会话开关开启时，除 deny 外全部免确认放行
    # （含危险命令，用户明确选择）。敏感文件（~/.claude/、.env）CC 内置保护层
    # 仍会强制原生确认，hook 的 allow 穿不过
    if auto_allow_enabled(event.get("session_id", "")):
        logger.info("自动放行模式放行: %s %s", tool_name, str(tool_input)[:200])
        emit_decision("allow", "HUD 自动放行模式开启")
        sys.exit(0)

    # 敏感文件（~/.claude/ 下、.env）：Claude Code 内置保护层会在 hook 之后强制原生
    # 确认，hook 的 allow 穿不过。HUD 再弹窗就成了双重确认，故直接转原生只确认一次，
    # 由 Notification hook 触发 HUD 提醒窗叫用户去终端
    if _is_sensitive_target(tool_name, tool_input):
        logger.info("敏感文件转原生确认: %s %s", tool_name, str(tool_input)[:200])
        emit_decision("ask", "敏感文件需在终端原生确认（HUD 不重复弹窗）")
        sys.exit(0)

    # decision == "ask" → 转 HUD
    logger.info("转 HUD 弹窗: %s %s", tool_name, str(tool_input)[:200])
    result = asyncio.run(ask_permission(event))

    if result is None:
        # HUD 不可用 → 不介入，让 Claude Code 走原生确认
        logger.warning("HUD 不可用，回退到 Claude Code 原生确认: %s", tool_name)
        sys.exit(0)

    approved, always_allow, fallback = result

    if fallback:
        # 超时未决策 / 用户点了"转终端" → 交回 Claude Code 原生确认（无限等待）
        logger.info("转原生确认: %s", tool_name)
        emit_decision("ask", "HUD 超时或用户选择转终端确认")
        sys.exit(0)

    if approved and always_allow:
        rule = build_allow_rule(tool_name, tool_input)
        if rule and _is_persistable_rule(rule):
            try:
                append_allow_rule(rule)
                logger.info("永久允许规则已写入: %s", rule)
            except Exception:
                logger.exception("写入永久允许规则失败: %s", rule)
        else:
            cmd_preview = (tool_input.get("command", "") if tool_name == "Bash"
                           else tool_name)
            logger.info("命令无法生成可持久化规则，本次放行但不写永久规则: %s",
                        str(cmd_preview)[:120])

    if approved:
        emit_decision("allow", "用户在 HUD 批准")
    else:
        logger.info("用户在 HUD 拒绝: %s %s", tool_name, str(tool_input)[:200])
        emit_decision("deny", f"用户在 HUD 拒绝了 {tool_name or '操作'} 操作")
    sys.exit(0)


if __name__ == "__main__":
    main()
