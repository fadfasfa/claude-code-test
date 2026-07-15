# Claude Code 入口

本文件只说明 Claude Code 在本仓的入口边界；不可违背规则见 `AGENTS.md`，通用需求澄清、编码纪律和自检由模型与当前规则直接完成。

## 使用方式

- 默认使用简体中文输出计划、执行进展、问题、风险、验证结果、审查结论和总结；生成计划文档、治理文档或任务总结时正文必须为简体中文，除非用户明确要求其他语言；英文计划视为不合格，交付前必须改为中文。
- 用户或材料明确要求其他语言时按该要求执行。
- 命令、路径、API、错误原文、分支名和技术标识符保持原文。
- 开始任务先运行 `git status --short`，发现非本轮修改时先报告并避免混入。
- 先读 `PROJECT.md`、`docs/index.md` 和任务相关 workflow 文档，再选择目标工作区。
- Plan 阶段默认走严格只读：只做 `Read` / 搜索 / 对话计划，不使用 `Bash`、`Write`、`Edit`、`MultiEdit`，不写 `.claude/plans/**`；执行前必须切到显式实现入口。
- 使用 GLM 或非 Anthropic first-party host 时，Plan 阶段必须按更保守口径处理：即使 UI 显示 `plan`，也不把 shell 视为只读保证。
- Claude Code 与 Codex 均可独立工作；无用户当前轮显性命令时，Claude Code 不调用、委派、审查或触发 Codex / CX。
- Git 授权、敏感文件、worktree、发布、discard 和完成报告规则全部引用 `AGENTS.md`，不得在本文件另写不同口径。
- `S/M/L` 仅作治理分级，不承担 bridge 执行职责。
- Git commit 与 PR 的语言与格式遵循 `docs/当前规则/20-Git与高危操作.md`，不在本文件另写口径。
- 长任务按需要拆分阶段并逐段自检；不因任务规模自动派子智能体或设置 reviewer 门禁。

## 退役说明

旧 CC-CX 强编排、Guard 状态机和 break-glass 流程已经退役，不作为 Claude Code 日常入口。
