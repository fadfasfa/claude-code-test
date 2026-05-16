# Repo Simplification Batch 1 Report

## Verdict

ACCEPTED

## Scope

本轮只做 `docs/workflows` active 层精简，核心是合并 Codex execution / proxy / surface / routing 口径，并删除一次性验收探针。

## What Changed

- 新增 `docs/plans/repo-simplification-execplan.md`。
- 新增 `docs/workflows/codex-execution-boundary.md`。
- 删除 `docs/workflows/03-model-routing.md`。
- 删除 `docs/workflows/06-codex-proxy-policy.md`。
- 删除 `docs/workflows/08-codex-surface-boundary.md`。
- 删除 `docs/workflows/_cc_cx_state_probe.md`。
- 更新 `docs/index.md`，增加新 canonical workflow 短入口。
- 更新 `PROJECT.md`，增加新 workflow 文件短引用。

## ExecPlan

- `docs/plans/repo-simplification-execplan.md`

## Probe Handling

- `_cc_cx_state_probe.md` 已删除。
- 删除原因：这是一次性强验收探针，不应留在 active workflow。
- 未归档：`docs/archive/reports/` 已有同类存证，本轮不重复归档。

## Codex Boundary Merge

- `03-model-routing.md`：存在，已合并。
- `06-codex-proxy-policy.md`：存在，已合并。
- `08-codex-surface-boundary.md`：存在，已合并。
- 合并目标：`docs/workflows/codex-execution-boundary.md`。
- 合并后去掉了 Codex App / VS Code Codex / Codex CLI / codex-proxy 的重复叙述，统一为执行面、proxy/额度边界、CC -> CX 契约和禁止事项。
- 未写入无法确认的 dashboard / usage 结论。

## Docs Index Update

- `docs/index.md` 仍为短入口，当前 18 行。
- 未复制合并正文。
- 未加入历史报告正文。

## Current Residual Status

本轮未处理以下 residual status：

```text
 D run/data/raw/hextech/Hextech_Data_2026-05-12.csv
?? .agyignore
?? run/data/raw/hextech/Hextech_Data_2026-05-16.csv
```

- Hextech CSV 属于业务数据，需要单独数据差异任务。
- `.agyignore` 是否纳入项目由用户另行决定。

## Validation

| Command | Result |
| :--- | :--- |
| `git status --short --untracked-files=all` | PASS；显示本轮 docs 变更和既有 residual status |
| `git diff --stat` | PASS；包含本轮 docs 删除/短引用更新，同时显示既有 `05-12.csv` deletion dirty |
| `git diff --name-status` | PASS；同上，待 staged 检查隔离业务数据 |
| `rg "03-model-routing|06-codex-proxy-policy|08-codex-surface-boundary" docs/index.md PROJECT.md AGENTS.md CLAUDE.md README.md docs/workflows` | PASS；无旧 active 入口引用 |
| `rg "codex-execution-boundary" docs/index.md PROJECT.md AGENTS.md CLAUDE.md README.md docs/workflows` | PASS；`docs/index.md` 和 `PROJECT.md` 指向新入口 |
| `rg "run/workflow|\\.workflow|\\.codex-exec-apple|\\.learnings" docs/workflows docs/index.md README.md PROJECT.md AGENTS.md CLAUDE.md` | PASS with allowed warnings；仅作为禁止事项、退休路径或不再放根目录说明出现 |
| `Test-Path docs/workflows/_cc_cx_state_probe.md` | PASS；False |
| `Test-Path docs/workflows/codex-execution-boundary.md` | PASS；True |
| `git diff --check` | PASS；仅 LF/CRLF 提示，无 whitespace failure |

## Risks

- Hextech CSV 仍需单独数据差异任务。
- `.agyignore` 是否应纳入项目需要用户另行确认。
- `agent_tooling_baseline.md` 和 `00-overview.md` 仍包含较多工具基线信息，后续可在 Batch 2 或专门 tooling docs 瘦身中处理。
- 后续 Batch 2/3/4/5 仍需分别验收，不能一次性混做。

## Next Batch Recommendation

- Batch 2：reference/archive 瘦身或 Hextech 数据差异任务。
- Batch 3：scripts 精简。
- Batch 4：skills 精简。
- Batch 5：verifier/hook 硬化。
