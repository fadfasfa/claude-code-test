# Git 语言与格式策略

本文件规定 Claude Code 与 Codex 在 claudecode 仓库内执行 commit、PR 时的语言与格式。授权边界见 `02-git-policy.md`，分支命名见 `worktree-policy.md`，本文件不重复这些规则。

## Commit Message

- 格式：`<中文type>(<可选 scope>): <中文描述>`。
- type 字典固定为：`修复`、`新增`、`文档`、`重构`、`杂项`、`试探`、`测试`。不使用英文 conventional 前缀。
- scope 可选；优先用中文模块名，原始目录名或包名可读性更高时保留英文（例如 `hextech`、`crawler`）。
- 首行不超过 72 字符，结尾不加句号。
- 正文与首行之间空一行；正文使用陈述句，必要时分行列出关键事实，不使用 emoji。

## 分支中文别名

- 新建分支后的首个 commit，正文必须包含独立一行：`分支: <英文分支名>（<中文别名>）`。
- 英文分支名沿用 `worktree-policy.md` 的 `cc/<type>/<slug>` 或 `codex/<type>/<slug>`，不变。
- 中文别名格式：`<中文type>-<中文 scope 或主题>`，例：`修复-抓取器污染`。
- 同一分支的后续 commit 不必重复该行。

## Pull Request

- 标题：与该 PR 主 commit 首行一致，即中文 conventional 格式。
- 正文使用三段中文小标题，顺序固定：`## 总结`、`## 验证`、`## 剩余风险`。
- 三段内容均使用陈述短句；无内容时写"无"，不留空标题。
- 不向仓库添加 `.github/pull_request_template.md`，避免影响非 agent 创建的 PR；本规则仅约束 agent。

## 例外

- GitHub Web UI 在 merge PR 时自动生成的 `Merge pull request #N from ...` commit 不改写。
- `git revert` / `git cherry-pick` 默认生成的 `Revert "..."` / `(cherry picked from ...)` 前缀保留英文；如手工补充说明，说明部分用中文。
- 第三方自动化（dependabot、pre-commit autofix、release bot 等）产生的英文 commit 与 PR 不在本规则约束内；agent 不主动改写其历史。
- 用户在当前轮明确要求"用英文 commit"或"沿用上游英文模板"时，该指令本身即例外授权，仅对该轮生效。

## 生效范围

- 仅约束本规则合并之后的新 commit 与新 PR。
- 不回填历史、不 rewrite 既有分支、不批量改旧 PR 标题。
