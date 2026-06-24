---
name: review
description: Claude Code 项目级 PR 审查入口；无参数且只有一个开放 PR 时直接审查，`all` 审查全部开放 PR。
disable-model-invocation: true
allowed-tools: [Bash, Read, Grep, Glob]
---

# review

你是本仓的代码审查者。此 skill 只读执行，不修改工作树、index、分支、远端或 PR 状态。

## 参数路由

1. 先读取并 trim `/review` 的原始参数。
2. 如果参数是 `all` 或 `--all`：
   - 运行 `gh pr list --state open --limit 100 --json number,title,author,baseRefName,headRefName,isDraft,updatedAt`。
   - 如果没有开放 PR，报告当前没有可审查 PR 并结束。
   - 如果存在开放 PR，逐个审查全部开放 PR。
3. 如果参数是 PR 编号：
   - 只审查该编号。
4. 如果没有参数：
   - 运行 `gh pr list --state open --limit 100 --json number,title,author,baseRefName,headRefName,isDraft,updatedAt`。
   - 如果没有开放 PR，报告当前没有可审查 PR 并结束。
   - 如果只有一个开放 PR，直接把它作为目标 PR 审查，不要要求用户再次输入编号。
   - 如果有多个开放 PR，列出编号、标题、分支和草稿状态，然后请用户用 `/review <number>` 或 `/review all` 指定范围，并结束本次命令。
5. 如果参数不是 `all` / `--all` / PR 编号，说明支持的用法是 `/review <number>` 或 `/review all`，并结束。

## 单个 PR 审查步骤

对每个目标 PR：

1. 运行 `gh pr view <number> --json title,body,author,baseRefName,headRefName,state,isDraft,additions,deletions,changedFiles,labels,commits,statusCheckRollup` 获取元数据。
2. 运行 `gh pr diff <number>` 获取 diff。
3. 必要时用只读命令查看相关文件、现有约定和测试入口；不要写文件，不要 staging，不要 commit，不要 push，不要 merge。
4. 输出审查意见，优先列出问题；没有发现问题时明确说没有发现阻断性问题，并说明剩余未验证风险。

## 审查重点

- 正确性、边界条件和回归风险。
- 是否符合本仓 `AGENTS.md`、`PROJECT.md`、`docs/index.md` 指向的工作流边界。
- 测试覆盖和验证证据是否足够。
- 安全、凭据、删除、发布、Git 高危操作风险。
- `run/**` 和 `QuantProject/**` 是否被触碰，以及是否符合当前 PR 范围。

## 输出格式

- 多个 PR 时按 PR 编号分节。
- 每个 PR 先给 `Findings`，按严重程度排序，包含文件和行号线索。
- 再给 `Open Questions / Assumptions`。
- 最后给一句简短摘要和建议验证命令。
