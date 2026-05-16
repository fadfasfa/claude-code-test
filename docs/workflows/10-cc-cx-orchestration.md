# CC / CX Orchestration

## 默认边界

- CC 只负责 planning、supervision 和 review。
- CX 的 CC 调用入口是根目录 `cx-exec.ps1`，它只转发到 `scripts/workflow/cx-exec.ps1`。
- `scripts/workflow/cx-exec.ps1` 使用 `C:\Users\apple\.codex-exec` 作为独立 `CODEX_HOME`，并优先调用 `C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`。
- 认证读取 `CODEX_PROXY_API_KEY` 环境变量；脚本不创建、不读取、不修改 `auth.json`。
- 流程层不维护 CC 状态字段；用户手动决定何时走纯 CX workflow。

## CC 调用参数

- `-TaskId`：任务目录名；缺省时由脚本生成。
- `-TaskDescription`：交给 Codex 的任务文本。
- `-Profile`：`design` / `implement` / `review` / `lint` / `full-access`，默认 `implement`。
- `-DryRun`：只写结构化 dry-run 结果，不调用真实 Codex，也不要求 proxy / wrapper preflight 成功。

## result.json

每个任务写入 `.state/workflow/tasks/<task_id>/result.json`，最小字段：

- 完成项
- 未完成项
- 已修改文件
- verify 结果
- local-review 结果
- 下一步建议

`cx-exec.ps1` 会把 stdout / stderr 分别写入 `.state/workflow/tasks/<task_id>/codex.log` 和 `.state/workflow/tasks/<task_id>/codex.err.log`，便于保留原始错误。

## CC Review

CC 审查不再要求根目录 `CLAUDE_REVIEW.md`。建议在 CC 自己的输出、`.state/workflow/reports/` 或 `docs/archive/reports/` 中记录：

- 审查结论：`PASS` / `BLOCK` / `COMMENT`
- 关键发现
- 必须修改项
- 建议修改项

## 纯 CX 路径

纯 CX workflow 不调用 `cx-exec.ps1`，不期待 `.state/workflow/tasks/<task_id>/result.json`。现有 `worktree-start -> verify -> local-review -> finalize-pr` 链路必须保持可用。

