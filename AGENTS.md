# Codex 项目入口

本项目同时支持 Codex 和 Claude Code。`CLAUDE.md` 继续作为项目架构和功能说明的唯一事实来源；本文件只提供 Codex 入口和平台差异，不复制整份说明。

## 开始工作

- 先读 `CLAUDE.md` 的「固定规则」「Git & GitHub 工作流」「项目简介与技术栈」「规范与约束」，再按任务读取相关功能章节；修改或运行测试前读「测试」章节。
- `CLAUDE.md` 很长，按标题搜索、分段读取，避免一次输出截断。不要把它直接复制或链接成 AGENTS.md，以免超过默认指令大小限制。
- 下列 Codex 差异覆盖 CLAUDE.md 中对应的 Claude 专用要求；其余项目规则继续适用。

## 本地语言调整（全局规则见 ~/.codex/AGENTS.md）

（暂无。这里只写和全局规则不同的地方。）

## Codex 与 Claude Code 的差异

- CLAUDE.md 中 R3、R5 的工作流责任同样适用于 Codex 主会话：在用户授权的任务范围内完成 Issue、分支、PR，CI 通过后自行合并；不直接推送 main。
- R14 的 Opus/Fable、Sonnet、`subagent_type: implementer` 和 `.claude/agents/implementer.md` 工具表是 Claude Code 专用配置，Codex 不照搬模型名称或参数。
- Codex 主会话可直接完成小任务；需要分工时，按当前会话实际提供的代理工具和模型执行。主会话负责设计、审查、验证及全部 Git/gh 操作。子代理任务必须写清背景、文件边界、验收方法及「不得执行 Git/gh」。
- 上述子代理禁令是任务约束，不等于工具权限隔离。不能因为 Claude 的 implementer 没有 Bash，就声称 Codex 子代理也没有 Shell；需要强制限制时先核实当前平台能力。
- 并行修改使用独立 worktree，不操作 Claude Code 管理的 `.claude/worktrees/`。已有未提交内容不得覆盖或清理。
- `.claude/settings.json`、`.claude/rules.txt`、Claude hooks、commands 和 MCP 设置不作为 Codex 自动加载的配置；不要直接复制它们到 Codex 设置。
- 语言规则使用 Codex 的全局 AGENTS.md。语言日志的说明以当前可用的 `language-log` 技能为准；不要手动重复写日志，也不要仅凭 Claude 配置就宣称 Codex hook 已启用。

## 必须保留的开发规则

- 开始任务先读取对应 Issue；新功能先建 Issue，再在分支上修改。Issue、PR、提交信息用中文，内容须能独立交接。
- 每个完整改动及时提交，审查 diff 并运行适合改动的检查；CI 失败不得合并。不要使用破坏性命令清理用户工作。
- Python + FastAPI + SQLite；前端直接修改 `static/`，没有 npm 构建步骤。
- 数据库访问只走 `database/` 包；测试数据库替换 `database.core.DB_PATH`，AI 测试替换 `ai._call_api`。
- 不在 8000 端口启动测试服务器；不修改真实用户数据来验证功能。
- 密钥只从环境变量读取；新增依赖同步 `requirements.txt`；不要吞掉异常或假装成功。
- 项目架构和功能变化继续记录在 `CLAUDE.md`；仅 Codex 平台差异写在本文件。

## 共用项目 skills

`.agents/skills/` 中的相对目录链接指向原有 `.claude/skills/`，两种工具共用同一份内容：

- `de-zh-bot`：生成符合本项目导入格式的中文词汇 YAML。
- `de-fr-bot`：生成符合本项目导入格式的法文词汇 YAML。

只有相关词汇任务才读取这些技能；它们的词条输出规则不改变普通开发对话的全局语言规则。同名全局技能与本项目用途不同时，明确读取本项目链接对应的 SKILL.md。

其他 Claude commands、agents 和插件不批量链接。`github-issue-editor` 中的 PR 禁令和标签流程与 CLAUDE.md 存在冲突，暂不接入；GitHub 开发流程遵循上面的项目规则。

参考：[AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)、[skills 与目录链接](https://learn.chatgpt.com/docs/build-skills)。
