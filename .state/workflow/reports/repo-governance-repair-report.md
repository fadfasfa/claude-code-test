# Repo Governance Repair Report

生成时间：2026-05-16

## Verdict

- ACCEPTED
- 范围说明：已完成审查报告中列出的安全修复；未 commit、未 push、未创建分支。
- 剩余口径：当前工作树仍包含前序遗留删除和未跟踪迁移文件，进入提交前必须人工复核。

## What Changed

- 迁移运行态：
  - `run/workflow/tasks/` -> `.state/workflow/tasks/`
  - `run/workflow/current/` -> `.state/workflow/current/`
  - `run/workflow/state/` -> `.state/workflow/state/`
  - `run/workflow/archive/` -> `.state/workflow/archive/`
- 归档报告：
  - `run/workflow/reports/*.md` -> `docs/archive/reports/`
  - `docs/workflows/_cc_cx_flow_probe.md` -> `docs/archive/reports/_cc_cx_flow_probe.md`
  - `docs/workflows/archives/2026-05-14_cc-cx-integration.md` -> `docs/archive/workflows/2026-05-14_cc-cx-integration.md`
- learning 分层：
  - `docs/learnings/LEARNINGS.md` -> `docs/reference/learnings/LEARNINGS.md`
  - `docs/learnings/FEATURE_REQUESTS.md` -> `docs/reference/learnings/FEATURE_REQUESTS.md`
  - `docs/learnings/ERRORS.md` -> `docs/archive/learnings-retired/ERRORS.md`
- docs 分层：
  - 新增 `docs/index.md`
  - 新增 `docs/reference/README.md`
  - 新增 `docs/archive/README.md`
  - 根层 policy 文档下沉到 `docs/reference/policies/`
- worktree 策略：
  - `docs/workflows/01-worktree-policy.md` -> `docs/workflows/worktree-policy.md`
  - 修正 `AGENTS.md`、`docs/workflows/00-overview.md` 和 worktree policy 中“写入任务默认创建 worktree”的冲突。
- executor 路径：
  - `scripts/workflow/cx-exec.ps1` 输出路径改为 `.state/workflow/tasks/<task_id>/`
  - executor 不再创建 `run/workflow/reports/`
  - `-DryRun` 不再要求 proxy / wrapper preflight 成功，也不调用真实 Codex。
- 注释和说明：
  - `cx-exec.ps1` 和 `scripts/workflow/cx-exec.ps1` 补充中文维护说明、输入输出、依赖路径和失败行为。
  - `scripts/workflow/tests/cx-exec.Tests.ps1` 补充中文说明。
  - 新增 `scripts/README.md` 和 `scripts/workflow/README.md`。
  - 新增 `docs/workflows/agent-skill-inventory.md`，列出保留 skill 的作用、触发场景、使用者和默认启用状态。

## Root Directory Before/After

Before:

- 根目录含入口文件、`.agents/`、`.claude/`、`.codex/`、`docs/`、`scripts/`、`run/`、独立项目目录。
- 运行态和报告实际残留在 `run/workflow/`。
- 根目录 `.workflow/`、`.codex-exec-apple/`、`.learnings/` 在 git 状态中已显示删除。

After:

- 根目录仍只保留入口、配置、一级功能区和独立项目目录。
- `run/workflow/` 已不存在。
- `.state/workflow/` 成为本地运行态根。
- `docs/` 形成 `index.md`、`workflows/`、`reference/`、`archive/` 四层。

## Boundary Decisions

| 区域 | 决策 |
| :--- | :--- |
| `.claude/` | 只服务 Claude Code；不是当前规则真相源，不主控 worktree。 |
| `.codex/` | 保留为空 Codex 占位；不恢复 repo-local `config.toml`，不放运行态。 |
| `.agents/` | 保留 7 个 Codex repo-local skill；通过 README 和 inventory 说明边界，不删除不确定 skill。 |
| `docs/` | 默认读 `docs/index.md` 和相关短规则；长文进入 reference/archive。 |
| `scripts/` | `scripts/workflow/` 是当前入口；`scripts/git/` 是 legacy/manual，不自动触发。 |
| `run/` | 回归 Hextech 业务运行区，不承载 workflow 设定、长期报告或 agent 规则。 |
| `.state/` | 本地运行态根；`tasks/current/state/archive/logs` ignored，`reports` 保留审查证据。 |
| learning | 根 `.learnings/` 不恢复；摘要进 reference，原始错误日志进 archive。 |
| worktree | 唯一当前策略在 `docs/workflows/worktree-policy.md`；默认不自动创建。 |

## Worktree Policy

- 谁可以创建 worktree：只有显式调用 `scripts/workflow/worktree-start.ps1 -Apply` 或 legacy/manual `scripts/git/ccw-new.ps1` 的人/脚本。
- 默认是否自动创建：否。
- 是否禁用重复创建：当前 policy 要求单一 active worktree；已有 dirty active worktree 时停止。
- 操作入口在哪里：当前入口是 `scripts/workflow/worktree-start.ps1`；`scripts/git/ccw-*.ps1` 仅 legacy/manual。
- CC/Codex 启动是否自动创建：未发现自动创建证据。

## Context Control

- `docs/index.md` 是 docs 的短入口。
- `docs/archive/` 默认不读，只用于历史审计。
- `docs/reference/` 默认不整体读取，只按任务点名读取具体文件。
- 历史报告从 `run/workflow/reports/` 迁到 `docs/archive/reports/`。
- 65KB 的 `ERRORS.md` 已从 active docs 层移到 `docs/archive/learnings-retired/`。
- `AGENTS.md` / `CLAUDE.md` 保持短规则，不复制长报告内容。

## Script Maintainability

- `cx-exec.ps1`：补充根 delegator 的中文参数、失败行为和不写运行产物说明。
- `scripts/workflow/cx-exec.ps1`：补充用途、输入输出、依赖路径、修改行为、失败行为；输出路径切到 `.state/workflow/tasks/`。
- `scripts/workflow/tests/cx-exec.Tests.ps1`：补充静态测试用途和只读边界。
- `scripts/README.md`：说明 `scripts/workflow/` 与 legacy `scripts/git/` 的分层。
- `scripts/workflow/README.md`：逐项说明当前 workflow 脚本作用和写入行为。

## Remaining Risks

- 当前 git 状态仍包含大量前序遗留删除：`.codex-exec-apple/**`、`.workflow/**`、`.learnings/LEARNINGS.md`、根临时产物、旧 `tests/workflow` 等。本轮没有恢复或提交这些删除。
- `git diff --stat` 不统计未跟踪新增文件；进入提交前必须用 `git status --short --untracked-files=all` 人工确认迁移成对关系。
- 旧历史报告中保留 `run/workflow`、`.workflow`、`.codex-exec-apple`、`.learnings` 等历史路径引用，这是 archive 证据，不是当前路径。
- 未运行 `local-review.ps1`，因为本轮目标明确要求根目录不应生成 `.task-worktree.json` / `TASK_HANDOFF.md`；当前 acceptance 应视为人工复核。

## Validation Result

| 检查 | 结果 |
| :--- | :--- |
| PowerShell parse: `cx-exec.ps1` | passed |
| PowerShell parse: `scripts/workflow/cx-exec.ps1` | passed |
| PowerShell parse: `scripts/workflow/tests/cx-exec.Tests.ps1` | passed |
| DryRun | `.\cx-exec.ps1 -TaskId "governance-dryrun" -TaskDescription "Dry run after repository governance cleanup" -Profile implement -DryRun` returned 0 |
| DryRun result | `.state/workflow/tasks/governance-dryrun/result.json`, status `success`, exit code `0` |
| Forbidden root residues | `.workflow`、`.codex-exec-apple`、`CODEX_RESULT.md`、`CLAUDE_REVIEW.md`、`TASK_HANDOFF.md`、`diagnosis.log`、`.task-worktree.json`、`.learnings` all absent |
| `run/workflow/tasks/` | absent |
| `run/workflow/` | absent |
| Active old-path scan | no current write path remained; only ignore rules, negative warnings, audit report, and archived historical reports contain old path names |
| `git status --short --untracked-files=all` | still dirty; includes this governance migration plus pre-existing deletions |
| `git diff --stat` | tracked diff reported 83 files changed, 407 insertions, 7349 deletions; untracked additions are visible only in `git status` |

## Next Step

- 建议重新跑 CC-CX 强验收，但应先让 CC 读取 `docs/index.md`、`docs/workflows/10-cc-cx-orchestration.md`、`docs/workflows/repository-layout.md` 和本修复报告。
- 进入 commit 前，建议先做人工 review：重点核对 `.codex-exec-apple/**`、`.workflow/**`、旧 root 临时产物、docs moved files 和 `.state/workflow/reports/*`。
- 不建议直接 commit/push；本轮已按要求没有 commit、没有 push、没有新建分支。
