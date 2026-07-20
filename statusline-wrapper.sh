#!/bin/bash
# claude-hud statusline 包装器：先跑原 claude-hud 插件输出状态栏，
# 再读悬浮球的 config.json，本会话被自动放行覆盖（全局开 或 本会话开）时
# 在末尾追加红色 [自动放行] 标记。不改插件 dist（更新会被覆盖），
# 开关状态与 permission.py hook 同源。
input=$(cat)

plugin_dir=$(ls -d "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/cache/claude-hud/claude-hud/*/ 2>/dev/null \
  | awk -F/ '{ print $(NF-1) "\t" $(0) }' \
  | sort -t. -k1,1n -k2,2n -k3,3n -k4,4n | tail -1 | cut -f2-)
# 自动探测 node：环境变量 > PATH > 候选路径（跨机通用，本机 /e/Adownloads/nodejs、远端 /d/Adownloads/software/node 均覆盖）
node_bin="${CLAUDE_HUD_NODE:-$(command -v node 2>/dev/null)}"
if [ -z "$node_bin" ] || ! [ -x "$node_bin" ]; then
  for cand in /e/Adownloads/nodejs/node /d/Adownloads/software/node/node "/c/Program Files/nodejs/node" /usr/bin/node; do
    [ -x "$cand" ] && node_bin="$cand" && break
  done
fi
out=$(printf '%s' "$input" | "$node_bin" "${plugin_dir}dist/index.js")

cfg="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/claude-hud/config.json"
sid=$(printf '%s' "$input" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)

covered=0
if grep -q '"auto_allow"[[:space:]]*:[[:space:]]*true' "$cfg" 2>/dev/null; then
  covered=1
elif [ -n "$sid" ] && grep -q "\"$sid\"[[:space:]]*:[[:space:]]*true" "$cfg" 2>/dev/null; then
  covered=1
fi

if [ "$covered" = "1" ]; then
  out="${out} $(printf '\033[1;91m')[自动放行]$(printf '\033[0m')"
fi
printf '%s' "$out"
