# Claude Code HUD 悬浮球

**当前版本**: 1.2
**更新日期**: 2026-06-12

一个 Windows 桌面悬浮球，用于同时监控多个 Claude Code 会话，并把权限确认从终端搬到桌面弹窗。基于 PyQt5，通过 Claude Code 的 hooks 机制驱动，无需修改 Claude Code 本体。

## 功能

- **多会话状态点**：悬浮球上每个会话一个圆点，颜色区分 工作中 / 等待 / 空闲 / 出错，悬停展开面板查看各会话详情（目录、当前工具、耗时）
- **点击聚焦终端**：点会话点自动把对应终端窗口带到前台（已处理 Win11 ConPTY 幻影窗委托），并气泡提示该会话目录；窗口已关闭时自动移除该点
- **弹弓小游戏**：按住会话点拖动会拉出一把弹弓，松手把点弹飞——抛物线飞行、屏幕边缘反弹、彗星拖尾，约 2.4 秒后自动飞回原位归队，纯属解压，不影响单击聚焦
- **赛博霓虹配色 + 玻璃球体**：深底霓虹全套主题，状态色一眼区分——工作草绿、等待明黄、待授权橙、出错粉红；悬浮球为玻璃质感胶囊体（半透明渐变 + 顶部高光）；权限弹窗、提醒窗、面板、菜单、弹弓统一风格
- **权限弹窗**：拦截需要确认的 Bash / Edit / Write 等工具调用，桌面弹窗提供 拒绝 / 转终端 / 允许 / 永久允许 四个选项，60 秒无人理会自动交回终端原生确认，鼠标悬停时暂停倒计时
- **全局快捷键**：权限弹窗期间在任意窗口按 Alt+Y 允许 / Alt+N 拒绝 / Alt+U 永久允许 / Alt+Enter 转终端（仅弹窗存在时注册热键，关闭即注销；多弹窗时亮边标识热键作用目标）；提醒窗期间 Esc 关闭、Alt+Enter 跳转对应终端
- **永久允许**：一键把当前命令写成 `settings.json` 的 allow 规则（危险命令只生成精确规则，不生成宽规则）
- **原生确认提醒**：遇到 hook 无法替代的原生确认（敏感文件等）时弹提醒窗，叫你回终端操作
- **历史记录**：右键悬浮球查看所有会话的工具调用历史（操作对象摘要一行一条）

## 架构

```
Claude Code hooks
  ├─ hooks/notify.py      状态上报（PreToolUse/PostToolUse/Stop/SessionStart/SessionEnd/Notification）
  │     └─ UDP 127.0.0.1:17891 → hud.py（fire-and-forget，hook 轻量退出）
  ├─ hooks/permission.py  权限决策（PreToolUse，匹配 Bash|Edit|Write|MultiEdit|NotebookEdit）
  │     └─ WS  127.0.0.1:17890 → hud.py 弹窗 → 决策按官方 hook 协议回传 Claude Code
  └─ hooks/hud_utils.py   两个 hook 共用：HUD 存活探测/拉起、跨进程文件锁

hud.py          PyQt5 常驻进程：悬浮球 + 弹窗 + WS/UDP 服务端，hook 发现未运行会自动拉起
start_hud.pyw   开机自启脚本（已运行则跳过）
sessions.json   会话状态文件（运行时生成，不提交）
```

## 权限决策逻辑

`permission.py` 复刻 Claude Code 原生语义，决策顺序：

| 顺序 | 规则 | 结果 |
| ---- | ---- | ---- |
| 1 | `permissions.deny` 命中（整条或任一段） | deny |
| 2 | `permissions.allow` 精确规则命中整条命令 | allow |
| 3 | 组合命令拆段（`;` `&&` `\|\|` `\|` `&`，重定向 `2>&1` 不拆），全部段命中 allow 规则或内置安全白名单 | allow |
| 4 | 含危险结构（重定向到文件等，`/dev/null` 与 fd 复制除外） | 弹窗 |
| 5 | 敏感目标（`~/.claude` 下文件、`.env`，含 Bash 命令引用其路径） | 转原生确认 |
| 6 | 其余 | 弹窗 |

`bypassPermissions` 模式下不拦截；`acceptEdits` 模式下编辑类工具不拦截，保持与原生行为一致。

## 安装

### 依赖

- Windows 10/11
- Python 3.9+，需要 `PyQt5`、`websockets`

```powershell
pip install PyQt5 websockets
```

### 接入

1. 把本仓库放到 `~/.claude/claude-hud/`
2. 在 `~/.claude/settings.json` 的 `hooks` 中加入（`python` 换成你的解释器路径）：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": ".*",
        "hooks": [{ "type": "command", "command": "python \"C:/Users/<用户名>/.claude/claude-hud/hooks/notify.py\"", "timeout": 5, "async": true }]
      },
      {
        "matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [{ "type": "command", "command": "python \"C:/Users/<用户名>/.claude/claude-hud/hooks/permission.py\"", "timeout": 600 }]
      }
    ],
    "PostToolUse": [
      { "matcher": ".*", "hooks": [{ "type": "command", "command": "python \"C:/Users/<用户名>/.claude/claude-hud/hooks/notify.py\"", "timeout": 5, "async": true }] }
    ],
    "Notification": [
      { "hooks": [{ "type": "command", "command": "python \"C:/Users/<用户名>/.claude/claude-hud/hooks/notify.py\"", "timeout": 5, "async": true }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "python \"C:/Users/<用户名>/.claude/claude-hud/hooks/notify.py\"", "timeout": 5, "async": true }] }
    ],
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "python \"C:/Users/<用户名>/.claude/claude-hud/hooks/notify.py\"", "timeout": 8, "async": true }] }
    ],
    "SessionEnd": [
      { "hooks": [{ "type": "command", "command": "python \"C:/Users/<用户名>/.claude/claude-hud/hooks/notify.py\"", "timeout": 5, "async": true }] }
    ]
  }
}
```

3. 启动任意 Claude Code 会话，hook 会自动拉起 HUD；或把 `start_hud.pyw` 加入开机自启

权限 hook 的 `timeout` 建议设大（如 600）：弹窗悬停会暂停倒计时，hook 超时太短会在你阅读长命令时被杀掉，导致点击丢失、原生确认重复弹出。无人理会时弹窗自身 60 秒就交回原生确认，不会真等满。

## 已知限制

- 仅支持 Windows（窗口聚焦、进程链定位用了 Win32 API）
- Windows Terminal 多标签页共用一个窗口句柄，系统没有定位标签页的接口，点击会话点只能聚焦到窗口级，弹窗会提示会话目录名，需手动切标签
- `~/.claude` 下文件和 `.env` 受 Claude Code 内置保护，hook 的 allow 穿不过，这类操作会转回终端原生确认（HUD 弹提醒窗）
- hooks 由系统 Python 运行，修改 `hooks/` 下脚本即时生效；修改 `hud.py` 需重启 HUD 进程

## 更新日志

### 1.2 (2026-06-12)

- 全局快捷键：权限弹窗 Alt+Y 允许 / Alt+N 拒绝 / Alt+U 永久允许 / Alt+Enter 转终端；提醒窗 Esc 关闭、Alt+Enter 跳转终端；多弹窗亮边标识热键目标
- 悬浮球玻璃质感胶囊体；状态绿调整为草绿，黄/橙提亮、紫加深
- 12 项稳定性/性能优化：UDP 监听自愈、跨线程/跨进程加锁、弹窗防重入、空闲停转动画、历史记录批量落盘、hooks 公共模块 hud_utils.py 等

### 1.1 (2026-06-11)

- 弹弓小游戏：会话点拖拽弹射，抛物线飞行、边缘反弹、彗星拖尾，自动归位
- 全套赛博霓虹配色：状态色、权限弹窗、提醒窗、会话面板、右键菜单、图标、弹弓统一深底霓虹风

### 1.0 (2026-06-11)

- 悬浮球多会话监控、状态色点、悬停面板、Logo 旋转动画
- 权限弹窗（拒绝/转终端/允许/永久允许、倒计时悬停暂停）
- 段级 Bash 权限匹配、危险结构识别、敏感目标转原生
- 点击会话点聚焦终端（含 ConPTY 幻影窗解析）、窗口关闭自动移除会话
- 工具调用历史（操作对象摘要）、原生确认提醒窗
