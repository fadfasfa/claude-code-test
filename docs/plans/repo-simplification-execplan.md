# Repo Simplification ExecPlan

## Goal

本轮只做 Batch 1：`docs/workflows` active 层精简。

## Source Review Summary

- 外部审查结论：设计稿 PASS conditional。
- 主要问题是 `03-model-routing.md`、`06-codex-proxy-policy.md`、`08-codex-surface-boundary.md` 三个 Codex/proxy/surface/routing 文件重复。
- `_cc_cx_state_probe.md` 是一次性探针，应删除。
- Hextech CSV 属于业务数据，不纳入本轮。

## Non-Goals

- Hextech CSV。
- `run/data`。
- `scripts` 主逻辑。
- `.agents/skills`。
- `.claude/skills`。
- `docs/archive/learnings-retired/ERRORS.md`。
- verifier hooks。
- 全仓大整理。
- `scripts/maintenance` 和 `scripts/archive` 空目录。

## Current Status

`git status --short --untracked-files=all`:

```text
 D run/data/raw/hextech/Hextech_Data_2026-05-12.csv
?? .agyignore
?? docs/workflows/_cc_cx_state_probe.md
?? run/data/raw/hextech/Hextech_Data_2026-05-16.csv
```

当前 residual status：

- `run/data/raw/hextech/Hextech_Data_2026-05-12.csv`：deleted，业务数据，本轮不处理。
- `.agyignore`：untracked，本轮不处理。
- `docs/workflows/_cc_cx_state_probe.md`：untracked，一次性探针，本轮删除。
- `run/data/raw/hextech/Hextech_Data_2026-05-16.csv`：untracked，业务数据，本轮不处理。

`docs/workflows` 当前文件：

| Name | Length |
| :--- | ---: |
| `_cc_cx_state_probe.md` | 463 |
| `00-overview.md` | 2455 |
| `02-git-policy.md` | 1106 |
| `03-model-routing.md` | 2558 |
| `04-verification-policy.md` | 867 |
| `05-pr-review-policy.md` | 917 |
| `06-codex-proxy-policy.md` | 2678 |
| `07-protected-assets.md` | 1177 |
| `08-codex-surface-boundary.md` | 1951 |
| `10-cc-cx-orchestration.md` | 1783 |
| `agent_tooling_baseline.md` | 5104 |
| `agent-skill-inventory.md` | 1636 |
| `cc-cx-collaboration.md` | 1994 |
| `repository-layout.md` | 4117 |
| `work_area_registry.md` | 4090 |
| `worktree-policy.md` | 1864 |

## Batch 1 Scope

允许修改：

- `docs/plans/repo-simplification-execplan.md`
- `docs/workflows/03-model-routing.md`
- `docs/workflows/06-codex-proxy-policy.md`
- `docs/workflows/08-codex-surface-boundary.md`
- `docs/workflows/codex-execution-boundary.md`
- `docs/workflows/_cc_cx_state_probe.md`
- `docs/index.md`
- `PROJECT.md`
- `AGENTS.md`，仅限增加 canonical source 短注释或更新短引用
- 必要时 `README.md` / `CLAUDE.md` 的短引用修正，但非必要不改
- `.state/workflow/reports/repo-simplification-batch1-report.md`

禁止修改：

- `run/data/**`
- `scripts/**`
- `.agents/skills/**`
- `.claude/skills/**`
- `docs/archive/learnings-retired/ERRORS.md`
- `.state/workflow/tasks/**`
- `.state/workflow/current/**`
- `.state/workflow/state/**`
- `.state/workflow/archive/**`
- `.state/workflow/logs/**`
- `.agyignore`

## Target Shape

- `docs/index.md` 是唯一默认入口。
- `docs/workflows/` 只放当前有效规则。
- `docs/reference/` 按需读取。
- `docs/archive/` 是历史证据，默认不读。
- Codex/proxy/surface/routing 当前规则统一到 `docs/workflows/codex-execution-boundary.md`。

## Decisions

- `_cc_cx_state_probe.md`：删除。
- `03-model-routing.md`、`06-codex-proxy-policy.md`、`08-codex-surface-boundary.md`：合并为 `codex-execution-boundary.md`。
- Hextech CSV：本轮不处理。
- `.agyignore`：本轮不处理，保持 untracked。
- `AGENTS.md`：如存在重复入口规则，只加 canonical source 注释，不扩写长文。

## Step-by-Step Plan

1. 读取实际存在的 `03-model-routing.md`、`06-codex-proxy-policy.md`、`08-codex-surface-boundary.md`。
2. 创建 `docs/workflows/codex-execution-boundary.md`，保留当前有效事实，删除重复口径和旧验收记录。
3. 删除实际存在的 `03-model-routing.md`、`06-codex-proxy-policy.md`、`08-codex-surface-boundary.md`。
4. 删除 `docs/workflows/_cc_cx_state_probe.md`，不归档。
5. 更新 `docs/index.md`，把 Codex execution boundary 作为当前短入口。
6. 仅在存在旧文件短引用时更新 `PROJECT.md`、`AGENTS.md`、`README.md`、`CLAUDE.md`。
7. 生成 `.state/workflow/reports/repo-simplification-batch1-report.md`。
8. 运行验收命令，确认业务数据、`.agyignore` 和禁止路径未进入本轮 staged diff。

## Validation Plan

```powershell
git status --short --untracked-files=all
git diff --stat
git diff --name-status
rg "03-model-routing|06-codex-proxy-policy|08-codex-surface-boundary|codex-execution-boundary" docs/index.md PROJECT.md AGENTS.md CLAUDE.md README.md docs/workflows
rg "run/workflow|\\.workflow|\\.codex-exec-apple|\\.learnings" docs/workflows docs/index.md README.md PROJECT.md AGENTS.md CLAUDE.md
Test-Path docs/workflows/_cc_cx_state_probe.md
Test-Path docs/workflows/codex-execution-boundary.md
```

## Commit Plan

如果验收通过：

```text
chore: simplify active workflow docs
```
