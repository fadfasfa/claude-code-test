<!-- locally-authored-minimal -->
---
name: verification-before-completion
description: Require concrete verification evidence before claiming a task is complete.
---

# verification-before-completion

中文简介：本 skill 用于任何实现任务完成前的验证关口。它要求提供具体 verification evidence；不负责强制 TDD、清理 worktree、修改 hooks 或改变全局 Claude Code settings。

## 什么时候使用

在声明任何 implementation task 完成前使用。

## 必要行为

1. 对影响 runtime paths 的行为变更、bug 修复或 refactor，先定位最近的现有 test；如果小型 regression test 可行，在改实现前添加或更新。
2. 对 docs、comments、read-only reviews 或纯组织调整，新增测试可选，但 final report 必须说明为什么没有加测试。
3. 验证选择顺序：nearest test -> changed module test -> smoke / fast verification -> full test suite。
4. test entrypoint 确认前只做只读探索；不安装依赖、不初始化新 test framework、不从网络下载、不改 global config。
5. 运行与变更最相关、最窄的验证命令，并检查输出，不假设成功。
6. final summary 报告精确命令、测试范围、结果和失败摘要；如果无法验证，说明准确原因。
7. 只要输出或 baseline comparison 支持，就区分 pre-existing failures 和当前变更引入的 failures。

## 读取失败 fallback

- 对 Markdown/text/code 文件，如果原生 `Read` 因 `pages` / `schema` / malformed / `Invalid pages parameter` 失败，一次即停止同类 `Read`；不得把 PDF `pages` 读取策略套用到这些文件。
- 改用只读路线，例如 scoped shell、PowerShell `Get-Content`、Python read-only script、`git show` / `git diff` 或更小范围读取。
- 如果 fallback 仍失败，报告 blocker，不猜测文件内容。

## 规则

- 没有证据时，永远不要宣称完成。
- 优先 repo-native commands 和已有 test scripts。
- 不添加 hooks、MCP requirements、worktrees、forced branch logic 或 TDD enforcement。
- 不为了测试而 clean、reset、stash、push、改 hooks、改 global Claude Code settings 或处理 locked worktrees。
