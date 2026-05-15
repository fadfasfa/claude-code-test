# CC / CX Orchestration

## 默认边界

- CC 只负责 planning、supervision 和 review。
- CX 执行入口只能是 `scripts/workflow/cx-exec.ps1`。
- `cx-exec.ps1` 使用 `C:\Users\apple\.codex-exec-cc` 作为独立 `CODEX_HOME`。
- 认证读取 `CODEX_PROXY_API_KEY` 环境变量；脚本不创建、不读取、不修改 `auth.json`。
- 流程层不维护 CC 状态字段；用户手动决定何时走纯 CX workflow。

## CODEX_PROMPT.md

最小字段：

- 目标
- 非目标
- 影响文件
- 验证方式
- 推荐 acceptance_gate
- 是否允许 CX 独立执行

## CODEX_RESULT.md

最小字段：

- 完成项
- 未完成项
- 已修改文件
- verify 结果
- local-review 结果
- 下一步建议

`cx-exec.ps1` 会把 `codex exec` 的 stdout / stderr 也写入该文件，便于保留原始错误。

## CLAUDE_REVIEW.md

最小字段：

- 审查结论：`PASS` / `BLOCK` / `COMMENT`
- 关键发现
- 必须修改项
- 建议修改项

## 纯 CX 路径

纯 CX workflow 不调用 `cx-exec.ps1`，不需要 `CODEX_PROMPT.md`，也不期待 `CLAUDE_REVIEW.md`。现有 `worktree-start -> verify -> local-review -> finalize-pr` 链路必须保持可用。
