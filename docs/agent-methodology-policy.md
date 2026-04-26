# Agent Methodology Policy

本文件定义 `claudecode` 仓内 Brainstorm、TDD 和本地 PR Review 的受控入口。它不是 Superpowers 全量安装说明，也不授权全局默认启用任何第三方方法包。

## 当前策略

1. Superpowers 当前不全量启用。
2. 如需未来试装，只允许 local scope / 单仓试验；不得 user/global 默认启用。
3. 不启用 SessionStart hook 类自动注入。
4. 不默认启用插件 `code-reviewer`。
5. Brainstorm / TDD / PR Review 采用本仓轻量命令和本地 agent 复刻必要能力。
6. 所有命令必须遵守现有权限边界、中文说明规则和卡帕西式简单代码规则。

## 受控入口

- `/brainstorm-task`：需求澄清、方案比较、风险列举；默认不写文件、不创建分支、不启动 worktree。
- `/tdd-task`：仅用于明确开发或修 bug；先定位现有测试体系，按最小 red-green-refactor 执行。
- `/review-pr-local`：本地只读 PR 审查入口，审查当前分支相对 base 的本地 git diff，替代云端 Codex PR Review。

## 边界

- 不安装全局 Superpowers。
- 不修改 `C:\Users\apple\.claude\settings.json`。
- 不放开裸 `git push*`。
- 不运行 `gh pr review --submit` 或 `gh pr merge`。
- 不让本地 PR Review agent 修改代码、提交、push 或向 GitHub 发布评论。
