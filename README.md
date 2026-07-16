# Claude Code HUD 悬浮球

**当前版本**: 1.3
**更新日期**: 2026-07-16

一个 Windows 桌面悬浮球，用于同时监控多个 Claude Code 会话，并把权限确认从终端搬到桌面弹窗。基于 PyQt5，通过 Claude Code 的 hooks 机制驱动，无需修改 Claude Code 本体。

## 功能

- **多会话状态点**：悬浮球上每个会话一个圆点，颜色区分 工作中 / 等待 / 空闲 / 出错，悬停展开面板查看各会话详情（目录、当前工具、耗时）
- **点击精确切换标签页**：点会话点自动把对应终端窗口带到前台（已处理 Win11 ConPTY 幻影窗委托），并进一步**切换到该会话所在的标签页**——Windows Terminal 多标签、VSCode Claude Code 扩展多会话均可精确定位（UIA 自动化 + 会话标签自学习）；PyCharm 等 Swing 界面不暴露自动化接口，只能聚焦到窗口级；窗口已关闭时自动移除该点
- **自动放行模式**：两级开关免权限确认——右键**会话点**只放行该会话（点带红圈），右键**图标区**全局放行（胶囊描边变红）；也可在任意会话内用 `/aa` 斜杠命令切换（配 `toggle_auto_allow.py`）；状态栏 wrapper 会在被放行会话的 statusline 末尾追加红色 `[自动放行]`；开关跨重启持久，deny 规则仍生效
- **会话自动摘点**：直接关掉终端标签（SessionEnd 不触发）也没关系——hook 记录 CC 主进程 PID，HUD 定期查活，进程退出后 1 分钟内自动移除对应的点
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
  ├─ hooks/notify.py      状态上报（PreToolUse/PostToolUse/Stop/SessionStart/SessionEnd/
  │     │                 Notification/UserPromptSubmit——敲回车瞬间补录终端窗口+学习激活标签）
  │     └─ UDP 127.0.0.1:17891 → hud.py（fire-and-forget，hook 轻量退出）
  ├─ hooks/permission.py  权限决策（PreToolUse，匹配 Bash|Edit|Write|MultiEdit|NotebookEdit），
  │     │                 先查 config.json 自动放行开关（全局/本会话）
  │     └─ WS  127.0.0.1:17890 → hud.py 弹窗 → 决策按官方 hook 协议回传 Claude Code
  └─ hooks/hud_utils.py   两个 hook 共用：HUD 存活探测/拉起、跨进程文件锁

hud.py                 PyQt5 常驻进程：悬浮球 + 弹窗 + WS/UDP 服务端 + UIA 标签切换/学习，
                       hook 发现未运行会自动拉起
toggle_auto_allow.py   /aa 斜杠命令后端：终端内切换自动放行（本会话/global/status）
statusline-wrapper.sh  包装 statusline 插件：被放行会话的状态栏追加红色 [自动放行]
start_hud.pyw          开机自启脚本（已运行则跳过）
config.json            自动放行开关状态（运行时生成，不提交）
sessions.json          会话状态文件（运行时生成，不提交）
```

## 权限决策逻辑

`permission.py` 复刻 Claude Code 原生语义，决策顺序：

| 顺序 | 规则 | 结果 |
| ---- | ---- | ---- |
| 1 | `permissions.deny` 命中（整条或任一段） | deny |
| 1.5 | 自动放行开关开启（全局或本会话，config.json） | allow |
| 2 | `permissions.allow` 精确规则命中整条命令 | allow |
| 3 | 组合命令拆段（`;` `&&` `\|\|` `\|` `&`，重定向 `2>&1` 不拆），全部段命中 allow 规则或内置安全白名单 | allow |
| 4 | 含危险结构（重定向到文件等，`/dev/null` 与 fd 复制除外） | 弹窗 |
| 5 | 敏感目标（`~/.claude` 下文件、`.env`，含 Bash 命令引用其路径） | 转原生确认 |
| 6 | 其余 | 弹窗 |

`bypassPermissions` 模式下不拦截；`acceptEdits` 模式下编辑类工具不拦截，保持与原生行为一致。

## 安装

### 依赖

- Windows 10/11
- Python 3.9+，需要 `PyQt5`、`websockets`、`uiautomation`（标签页精确切换用，缺省时退化为窗口级聚焦）

```powershell
pip install PyQt5 websockets uiautomation
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
    ],
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "python \"C:/Users/<用户名>/.claude/claude-hud/hooks/notify.py\"", "timeout": 5, "async": true }] }
    ]
  }
}
```

> `python` 务必写解释器**绝对路径**：hook 跟随会话终端的 PATH，PyCharm/venv 终端里裸 `python` 可能指向缺依赖的解释器导致 hook 静默失败（会话不上报、没有小球点）。

可选：`/aa` 斜杠命令（终端内切换自动放行）——新建 `~/.claude/commands/aa.md`，内容调用 `toggle_auto_allow.py` 并传入 `$CLAUDE_CODE_SESSION_ID`。

3. 启动任意 Claude Code 会话，hook 会自动拉起 HUD；或把 `start_hud.pyw` 加入开机自启

权限 hook 的 `timeout` 建议设大（如 600）：弹窗悬停会暂停倒计时，hook 超时太短会在你阅读长命令时被杀掉，导致点击丢失、原生确认重复弹出。无人理会时弹窗自身 60 秒就交回原生确认，不会真等满。

## 已知限制

- 仅支持 Windows（窗口聚焦、进程链定位、UIA 自动化用了 Win32 API）
- 标签页精确切换依赖"标签自学习"：会话至少发过一条消息（UserPromptSubmit 时认领激活标签）或干过活（WT 转轮认领）后才能精确定位，此前点击退化为窗口级聚焦并提示
- PyCharm 等 JetBrains IDE 为 Swing 界面、不暴露 UIA 控件树（无障碍走 Java Access Bridge），只能聚焦到 IDE 窗口级
- `~/.claude` 下文件和 `.env` 受 Claude Code 内置保护，hook 的 allow 穿不过，这类操作会转回终端原生确认（HUD 弹提醒窗）；自动放行模式同样穿不过
- hooks 每次触发新进程，修改 `hooks/` 下脚本即时生效；修改 `hud.py` 需重启 HUD 进程

## 更新日志

### 1.3 (2026-07-16)

- **自动放行模式**：全局/单会话两级开关（悬浮球右键、`/aa` 斜杠命令），开启后除 deny 规则外免权限确认；球圈/描边变红 + 状态栏红色 `[自动放行]` 三处状态同步，跨重启持久
- **点击精确切换标签页**：WT 多标签（转轮字符认领学习）与 VSCode Claude Code 扩展（激活标签认领 + 窗口标题双源学习）均可切到具体会话；处理 Chromium 无障碍树冷启动、Select 静默无效（校验 IsSelected 逐级升级 Invoke/DoDefaultAction/真实点击）、组内选中≠最前等坑；学习结果持久化
- **会话自动摘点**：记录 CC 主进程 PID 定期查活，直接关终端标签也能 1 分钟内摘点，不再等 1 小时兜底
- 新增 UserPromptSubmit hook：敲回车瞬间补录终端窗口（前台窗口兜底，解决 IDE pty 引导进程父链断裂）+ 学习激活标签
- 权限弹窗"永久允许"规则预览、组合命令公共前缀规则、跳过前导环境变量赋值等永久允许重构（2026-07-10 批次）

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
