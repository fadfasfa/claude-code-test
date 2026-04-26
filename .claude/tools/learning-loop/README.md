# Self-Improvement Review Input

中文简介：本目录提供 repo-local self-improvement 的只读检查入口。它用于人工审查 learning/error/hook wiring；不自动晋升 learning，不修改 rules，不写 global/kb。

从仓库根目录运行：

```powershell
python .claude/tools/learning-loop/check_learning_loop.py
```

从本目录运行：

```powershell
python check_learning_loop.py
```

- 这个检查是只读的。
- `.learnings/LEARNINGS.md` 是本仓 tracked evolution log。
- `.learnings/ERRORS.md` 是 ignored local/raw error input，不是主入口。
- 它可以查看重复错误并提出 CC skill 变更建议。
- 只有在用户授权任务内，才可以执行获批的 repo-local CC skill 变更。
- 它可以提出 rule、skill、hook 或 global sync 建议；但 global sync 必须先人工审查。
- 每次 self-improvement evolution 都必须向 `.learnings/LEARNINGS.md` 追加独立条目。
- 它不得 auto-loop、auto-edit rules、创建 global skills、写 global config、修改 `kb`，也不得干扰 CX App / Codex memory。
- 一次性经验没有明确批准时，不得晋升为长期规则。
- Repo-local hooks 可以追加 failure records，供后续人工 review。
