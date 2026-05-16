# PR Review Policy

## 本地优先

- 默认先做本地 review，不调用云端 PR。
- 使用 `scripts/workflow/local-review.ps1` 输出 diff 摘要、风险和建议验证。
- 只有用户明确要求时才进入远端 PR 流程。

## 发布边界

- `finalize-pr.ps1` 默认 dry-run。
- review branch、commit、push、PR create 都需要明确且独立的授权。
- push 和 PR 必须二次确认；merge 不进入默认自动路径。
- `manual-required` 且没有 `manual_accepted` 时，不得 commit。
- `automated` 只允许进入 finalize dry-run / review branch / commit 授权阶段，不自动放行 push / PR / merge。

## Review 内容

- 修改范围是否符合 `docs/workflows/work_area_registry.md`。
- 是否触碰受保护路径。
- 是否有验证证据。
- 是否有未说明的风险。
- changed files 是否都在 `target_paths` / `allowed_paths` 内。

