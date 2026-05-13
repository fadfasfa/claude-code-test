# PR Review Policy

## 本地优先

- 默认先做本地 review，不调用云端 PR。
- 使用 `scripts/workflow/local-review.ps1` 输出 diff 摘要、风险和建议验证。
- 只有用户明确要求时才进入远端 PR 流程。

## 发布边界

- `finalize-pr.ps1` 默认 dry-run。
- git add、commit、push、PR create 都需要明确授权。
- push 和 PR 必须二次确认。

## Review 内容

- 修改范围是否符合 `work_area_registry.md`。
- 是否触碰受保护路径。
- 是否有验证证据。
- 是否有未说明的风险。
