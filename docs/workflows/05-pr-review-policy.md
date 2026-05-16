# PR Review Policy

## Local First

- 默认先做本地 review，不调用云端 PR。
- 使用 `scripts/workflow/local-review.ps1` 输出 diff 摘要、风险和建议验证。
- 只有用户明确要求时才进入远端 PR、GitHub Actions 或云端评论流程。

## Publishing Boundary

- `scripts/workflow/finalize-pr.ps1` 是发布前辅助脚本，默认 dry-run。
- review branch、commit、push 和 PR create 都需要明确且独立的用户授权。
- merge、amend、tag 或 release 不进入默认自动路径。

## Review Checks

- 修改范围是否符合 `docs/workflows/work_area_registry.md`。
- 是否触碰受保护路径。
- 是否有验证证据和未说明风险。
- staged 清单是否只包含本轮授权范围。
