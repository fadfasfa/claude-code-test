# Claude Code 仓库入口

本文件只负责把 Claude Code 路由到本仓事实源和正确权限入口；通用协作方式由全局 `CLAUDE.md` 维护，不在这里复制第二份。

## 启动顺序

- 开始任务先运行 `git status --short`；发现非本轮修改时先报告、绕开且不得混入。
- 依次读取 `PROJECT.md`、`docs/index.md` 和任务相关短规则，再从 `docs/当前规则/10-工作区登记.md` 选择明确工作区。
- 进入业务工作区后继续读取其本地文档索引和任务事实源；仓库根目录只承载治理、路由与工具骨架。
- 修复、排查、重构任务开工前，先查 `C:\Users\apple\kb\03 AI学习与实操\代码仓库维护\AI对话-踩坑速查.md`（历史会话踩坑索引，`needs-review`），命中同主题再读对应簇页。
- 默认使用简体中文；技术标识符、路径、命令、API、分支名和错误原文保持原文。

## Plan 与实施入口

- Anthropic first-party 使用 Claude Code 原生 Plan：允许受控只读探索，并把正式计划写入系统分配的 `~/.claude/plans/*.md`；不得在 Plan 阶段修改仓库业务文件。
- GLM 或其他兼容 provider 必须使用 `claude --permission-mode plan --settings .claude/settings.plan.json`；该入口禁用 shell 和业务文件写入，只放行原生计划目录。
- 需求基线获批后才写正式计划；Plan Review UI 或明确文字批准后，使用 `claude --permission-mode acceptEdits --settings .claude/settings.implement.json` 进入显式实施入口。
- 同一会话的正式计划在原生文件中完整修订并保留历史，默认不提交。VS Code 未自动打开时使用 `Ctrl+G` 或计划文件路径审查。
- 不切换或维护 provider、账号池、token、proxy 和认证配置；这类工作必须作为单独任务处理。

## 仓库边界

- Git、高危操作、保护资产、worktree、验证和完成报告遵循 `AGENTS.md` 及 `docs/当前规则/` 对应事实源，不在本文件另写不同口径。
- Claude Code 与 Codex 均可独立工作；只有用户当前请求明确点名 `Codex`、`CX` 或对应命令时，Claude Code 才可调用或委派。
- 不恢复旧 CC-CX 强编排、Guard 状态机、通用流程 Skill、hook、wrapper 或自动 reviewer 门禁。
