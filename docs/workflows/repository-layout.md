# Repository Layout

## 设计原则

根目录只保留入口、配置、一级功能区和独立仓库目录。普通 Codex 修改只产出目标 diff 和对话摘要；运行态结果、任务日志和一次性验证痕迹集中放入 `.state/workflow/`，避免根目录和 `run/` 被任务过程产物淹没。

## 为什么运行态集中在 `.state/workflow/`

`run/` 是业务运行区和数据区，不承载仓库级 workflow 规则、长期报告或 agent 规则。`docs/` 不接收运行态；`docs/plans/` 和 `docs/archive/reports/` 不是普通任务默认输出位置。CC -> CX 调用产生的 `result.json`、stdout/stderr 日志和滚动状态都属于本地运行态，应放在默认 git ignored 的 `.state/workflow/`。

`.state/workflow/` 的职责：

- `.state/workflow/tasks/`：每次 CC -> CX 调用的结构化结果和日志，默认 ignored。
- `.state/workflow/reports/`：仅用于审查、验收、事故复盘或 commit 前人工复核，不是普通任务默认输出位置。
- `.state/workflow/archive/`：旧运行态和过期任务状态，默认 ignored。
- `.state/workflow/current/`：当前流程滚动状态，默认 ignored；可覆盖，不应每轮新建不同名字的 summary/report 文件。
- `.state/workflow/state/`：脚本状态文件，默认 ignored。
- `.state/workflow/logs/`：运行日志，默认 ignored。

## 为什么不再使用根目录 `.workflow`

根目录 `.workflow` 会让运行态和入口层混在一起。它已经退休；新脚本和文档不得再把 `.workflow/` 当作当前路径。历史报告里出现 `.workflow/` 只表示当时状态。

## `.codex-exec-apple`

`.codex-exec-apple` 是旧相对 `CODEX_HOME` 运行残留，包含 sessions、skills、sqlite 状态和临时缓存。当前正式 CC executor 使用 `C:\Users\apple\.codex-exec`，仓库根目录不应再出现 `.codex-exec-apple`。

## CC -> CX 结果位置

CC 调用根目录入口：

```powershell
.\cx-exec.ps1 -TaskId "<task-id>" -TaskDescription "<task>" -Profile implement
```

结果位置：

```text
.state/workflow/tasks/<task_id>/result.json
.state/workflow/tasks/<task_id>/codex.log
.state/workflow/tasks/<task_id>/codex.err.log
```

## Codex 独立工作 vs CC 调用

- Codex 独立工作：用户直接给 Codex 下任务；Codex 读取 `AGENTS.md`、`PROJECT.md`、`docs/workflows/work_area_registry.md` 和用户 prompt，不需要 `cx-exec.ps1`。
- CC 调用工作：Claude Code 负责规划、监督和验收；通过 `cx-exec.ps1` 调用 CX；CX 只做读代码、写代码、跑命令和产出结构化结果。

## 目录职责

- `.agents/`：仓库级 Codex skill 白名单和桥接说明。
- `.claude/`：Claude Code 占位和必要本地接口，不是规则真相源。
- `.codex/`：Codex 项目配置占位，不放运行态 CODEX_HOME。
- `.githooks/`：本仓 Git hooks。
- `docs/`：短索引、规则、路由、安全、工作流、reference 和 archive。
- `docs/reference/learnings/`：保留仍有价值的 learning 摘要。
- `docs/archive/learnings-retired/`：退休的原始错误输入。
- `docs/workflows/`：工作流规则、注册表、工具基线和协作说明。
- `run/`：业务运行区，不承载仓库级 workflow 运行态。
- `.state/workflow/`：CC -> CX 滚动状态和机器运行态，不是长期文档区。
- `scripts/`：仓库级脚本。
- `scripts/workflow/`：workflow 脚本和脚本测试。
- `heybox/`、`qm-run-demo/`、`QuantProject/`、`sm2-randomizer/`、`subtitle_extractor/`：独立仓库目录，不做目录治理迁移。

## 不要乱动

- 五个独立仓库目录：`heybox/`、`qm-run-demo/`、`QuantProject/`、`sm2-randomizer/`、`subtitle_extractor/`
- 入口规则：`AGENTS.md`、`CLAUDE.md`、`PROJECT.md`、`README.md`
- CC -> CX 入口：`cx-exec.ps1`、`scripts/workflow/cx-exec.ps1`
- VS Code wrapper：`C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`
- 全局 CODEX_HOME：`C:\Users\apple\.codex-exec`

## 可以清理

- `.state/workflow/tasks/` 中过期任务结果。
- `.state/workflow/current/` 和 `.state/workflow/state/` 中过期状态。
- `.state/workflow/archive/` 中经确认不再需要的旧运行态。
- 不应再出现的根目录残留：`.workflow/`、`.codex-exec-apple/`、`.learnings/`、`tests/workflow/`、`CODEX_RESULT.md`、`CLAUDE_REVIEW.md`、`TASK_HANDOFF.md`、`diagnosis.log`、`.task-worktree.json`。
