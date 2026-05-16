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

## Artifact Contract

- 普通 Codex 修改任务默认不生成 `docs/plans/*.md`、Markdown report、临时 probe 或 archive 证据文件；任务结束摘要默认留在对话里。
- `.state/workflow/current/` 是滚动状态区，只允许覆盖 `task.json`、`summary.md`、`review.md`、`handoff.md`，默认不提交。
- `.state/workflow/tasks/<task_id>/` 是机器运行态，只保存 `result.json`、`codex.log`、`codex.err.log`，默认 ignored，验收后可以清理。
- `.state/workflow/reports/` 只用于审查、验收、事故复盘或 commit 前人工复核；不得作为普通修改任务的默认输出。
- `docs/archive/reports/` 和 `docs/plans/` 只在用户确认需要长期留档时使用。
- probe 文件不得留在 `docs/workflows/` active 层；如需存证，只写入审查摘要，不保留 probe 本体。

## CC Review

CC 审查不再要求根目录 `CLAUDE_REVIEW.md`。审查、验收、事故复盘或 commit 前人工复核可以在 CC 自己的输出或 `.state/workflow/reports/` 中记录；只有用户确认需要长期留档时，才晋升到 `docs/archive/reports/`：

- 审查结论：`PASS` / `BLOCK` / `COMMENT`
- 关键发现
- 必须修改项
- 建议修改项

## 纯 CX 路径

纯 CX workflow 不调用 `cx-exec.ps1`，不期待 `.state/workflow/tasks/<task_id>/result.json`。现有 `worktree-start -> verify -> local-review -> finalize-pr` 链路必须保持可用。

