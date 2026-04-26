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

- 不用本仓重构全局 Claude Code / Codex 配置。
- 不用本仓重构 `kb`。
- 不把 coding-only 工作流推广到 `kb`。
- 不让 Superpowers、Playwright、TDD、worktree、PR 或 subagent 流程成为所有任务的强制默认项；ECC 已剔除，不作为默认能力或候选模块恢复。
