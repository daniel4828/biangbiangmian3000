#!/usr/bin/env bash
# 语言错误日志（#1129）：UserPromptSubmit hook 调用，把 Daniel 的原话和纠正
# 追加进 language-log/<日期>-<会话 id>.md，每个对话一个文件。
#
# 为什么用 hook 而不是让主会话自己写文件：主会话每条消息都要记得做这件事，
# 既占注意力又一定会漏（R11 已经是一条"绝不可跳过"的规则了，再加一条只会更糟）。
# 纠正由一次便宜的 Haiku 调用生成，跑在后台，主回答完全不参与。
#
# 🔴 递归防护两道：① CLAUDE_LANG_LOG=1 环境变量（嵌套的 claude 进程会继承它，
# 它的 hook 一进来就退出）；② 嵌套调用 cd 到 /tmp，不加载本项目的 .claude/settings.json。
set -uo pipefail

[ -n "${CLAUDE_LANG_LOG:-}" ] && exit 0

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$PROJECT_DIR/language-log"

payload="$(cat)"
prompt="$(printf '%s' "$payload" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("prompt",""))')"
session="$(printf '%s' "$payload" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))')"

# 跳过：空消息、斜杠命令、粘贴的报错/代码（多行且含大量非中文标记）
[ -z "${prompt//[[:space:]]/}" ] && exit 0
case "$prompt" in
  /*) exit 0 ;;
esac

mkdir -p "$LOG_DIR"
FILE="$LOG_DIR/$(date +%Y-%m-%d)-${session:0:8}.md"
[ -f "$FILE" ] || printf '# 语言错误日志 %s（会话 %s）\n' "$(date +%Y-%m-%d)" "${session:0:8}" > "$FILE"

INSTRUCTION='你是中文老师。下面是学生（德语母语，中文 HSK4-5）写给 AI 助手的一条消息，可能混着德语/英语/法语。

只输出他中文表达上的错误，每行一条，格式严格如下（不要表情符号、不要任何别的文字）：
- ~~错误写法~~ → 正确写法（拼音 - 德语解释）

规则：
- 外语词（如 "implement"、"feature"）算错误，给出中文说法
- 只有拼写/语法/用词错误才列；风格偏好不列
- 整条消息都正确时，只输出一个字：无
- 消息是粘贴的终端输出、报错或代码时，只输出一个字：无

消息如下：
'

corrections="$(cd /tmp && CLAUDE_LANG_LOG=1 claude -p --model haiku "$INSTRUCTION$prompt" 2>/dev/null)"

{
  printf '\n## %s\n\n' "$(date +%H:%M)"
  printf '**原话：** %s\n\n' "$prompt"
  if [ -z "${corrections//[[:space:]]/}" ]; then
    printf '（纠正生成失败）\n'
  elif [ "${corrections//[[:space:]]/}" = "无" ]; then
    printf '（没有错误）\n'
  else
    printf '%s\n' "$corrections"
  fi
} >> "$FILE"
