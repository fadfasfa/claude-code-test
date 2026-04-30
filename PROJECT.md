# claudecode 项目地图

`claudecode` 是一个多功能本地开发仓库。它不是单一应用，也不是知识库工作流仓库。

## 根目录职责

仓库根目录包含：

- 稳定入口文件：`AGENTS.md`、`CLAUDE.md`、`PROJECT.md`、`README.md`
- 工作区注册表：`work_area_registry.md`
- 能力基线：`agent_tooling_baseline.md`
- repo-local 工作流文档：`docs/`
- repo-local Claude Code 表面：`.claude/settings.json`、`.claude/hooks/`、`.claude/skills/`、`.claude/tools/`

根目录文件只用于仓库治理和工作流重构。业务实现应落在选定工作区内。

当前工作流目标：Claude Code 是本仓日常主力开发端；Codex 用于全局 / 跨仓库能力、重构辅助、个人助理型任务和云端 PR 审查；Web 端 AI 只提供需求、知识和意见参考；Antigravity 只由人工显式触发做独立审查或高难前端落地。

## 工作区

| 路径 | 职责 |
| :--- | :--- |
| `run/` | Hextech runtime、scraping、processing、display 和 tools |
| `sm2-randomizer/` | Space Marine 2 randomizer 应用和数据 pipeline |
| `QuantProject/` | 量化策略 / 数据工作区 |
| `heybox/` | 本地 scraping 脚本 |
| `qm-run-demo/` | demo/runtime 变体工作区 |
| `subtitle_extractor/` | 字幕提取工作区 |
| `.claude/` | repo-local Claude Code settings、skills、hooks 和 tools |
| `.learnings/` | repo-local learning 记录和 ignored raw error 输入 |
| `.tmp/` | ignored 运行态临时空间 |
| `docs/` | repo-local 工作流、安全、路由和验证策略 |

业务写入前，先以 `work_area_registry.md` 作为稳定注册表。

如果用户已经指定工作区，例如 `run/`，先只检索 `work_area_registry.md` 中该工作区条目，再列该工作区一级目录。不要把 `Glob run/**/*` 或同类全量递归搜索作为第一步；先按 `display`、`processing`、`scraping`、`tools` 等子区缩小范围，只有明确需要时才扩大 Glob。

## 工作流文档

| 文件 | 用途 |
| :--- | :--- |
| `docs/task-routing.md` | 小 / 中 / 大任务路由 |
| `docs/safety-boundaries.md` | 仓库、git、hook、global、kb 安全边界 |
| `docs/module-admission.md` | 模块准入卡模板和当前模块卡 |
| `docs/continuous-execution.md` | active-task ledger 与 resume/stop 规则 |
| `docs/frontend-validation.md` | 轻量前端验证工作流 |
| `docs/playwright-policy.md` | claudecode-only Playwright 策略 |
| `docs/git-worktree-policy.md` | 详细 worktree 分类和护栏 |

## 非目标

- 普通业务任务不用本仓重构全局 Claude Code / Codex 配置；只有用户明确发起全局层治理任务时才进入。
- 不用本仓重构 `kb`。
- 不把 coding-only 工作流推广到 `kb`。
- 不让 Superpowers、Playwright、TDD、worktree、PR 或 subagent 流程成为所有任务的强制默认项；ECC 已剔除，不作为默认能力或候选模块恢复。
- 不恢复 `.ai_workflow`、`finish-task`、`event_log`、`active_tasks_index` 或旧 Hextech 状态机。
- 不创建 lowercase `agents.md`，不创建 `.claude/tasks/current.md` 或其他轻量任务便签。
