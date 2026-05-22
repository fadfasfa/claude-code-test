# Claude Code 入口

本文件只说明 Claude Code 在本仓的入口边界；不可违背规则见 `AGENTS.md`，完整 S/M/L 与 Superpowers 执行流程见 `.agents/skills/superpowers-project-bridge/SKILL.md`。

## 使用方式

- 默认使用简体中文。
- 开始任务先运行 `git status --short`，发现非本轮修改时先报告并避免混入。
- 先读 `PROJECT.md`、`docs/index.md` 和任务相关 workflow 文档，再选择目标工作区。
- Claude Code 与 Codex 均可独立工作；无用户当前轮显性命令时，Claude Code 不调用、委派、审查或触发 Codex / CX。
- Git 授权、敏感文件、worktree、发布、discard 和完成报告规则全部引用 `AGENTS.md`，不得在本文件另写不同口径。

## 退役说明

旧 CC-CX 强编排、Guard 状态机和 break-glass 流程已经退役，不作为 Claude Code 日常入口。
