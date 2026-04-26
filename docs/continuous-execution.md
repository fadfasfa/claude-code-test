# 连续执行治理

本文件说明 Claude Code 在计划被确认后如何持续推进到明确终点，同时保留安全边界。

## 运行态 Ledger

路径：

```text
.tmp/active-task/current.md
```

这个 ledger 是 ignored 运行态记录。它不是规则层，不是 learning，不是提交产物，也不是授权凭证。

推荐字段：

```markdown
# Active Task

- User goal:
- Accepted plan:
- Current phase:
- Completed:
- Next safe step:
- Blockers:
- Files in scope:
- Files explicitly out of scope:
- Verification plan:
- Dangerous operations still requiring confirmation:
- Resume notes:
```

## 可以自动继续的情况

同时满足以下条件时，可以不重复询问并继续执行：

- 用户已经接受计划。
- 下一步仍在已接受范围内。
- 写入路径是 `C:\Users\apple\claudecode` 内的当前任务文件。
- 不需要安装、卸载或升级依赖。
- 不需要写全局层或 `kb`。
- 不涉及危险 git 操作。
- dirty-tree 所属关系清晰。
- 验证命令是范围内的 read/status/test/smoke 命令。

## 必须停下询问的情况

出现以下情况时，必须停下等待用户确认：

- 范围变化。
- 目标工作区不清楚。
- dirty tree 在同一文件里混有无关用户改动。
- 需要安装、卸载或升级依赖。
- 需要修改全局层或 `kb`。
- 需要新增 Playwright config/script/hook/tool，但还没有模块准入卡。
- git 操作是 `push`、PR、`merge`、`reset`、`clean`、`rebase`、`stash` 或 `worktree remove`。
- `git add` / `git commit` 缺少计划授权、清晰 diff 范围或已确认 message。

## Blocker 报告

无法安全继续时，生成 blocker 报告。

报告应包含：

- 被阻塞的步骤
- 准确原因
- 涉及的文件 / 命令
- 必要时包含当前仓库状态
- 安全选项
- 需要用户确认的内容

## 交接稿

以下情况需要生成交接稿：

- 上下文过长。
- 用户暂停任务。
- 长命令或外部进程无法在本轮完成。
- 工作需要转移到另一轮会话。

交接稿应包含目标、已接受计划、已完成项、当前触碰文件、已运行验证、下一步和剩余确认项。

## 中断后恢复

恢复时：

1. 读取 `AGENTS.md`、`CLAUDE.md` 和本文件。
2. 如果 `.tmp/active-task/current.md` 存在，读取它。
3. 运行 `git status --short --branch`。
4. 将 ledger 范围和当前 dirty tree 对照。
5. 只有下一步仍安全且在范围内时才继续。
6. 否则生成 blocker 或交接稿。

## Stop Hook 策略

`stop-guard-lite` 在明确批准前只是候选模块。

如果后续获批：

- Stop hook 可以读取 `.tmp/active-task/current.md`。
- 如果 ledger 显示计划未完成，它只能提醒 agent 继续、写交接稿或说明 blocker。
- 它不得自动运行命令。
- 它不得自动继续会话。
- 它不得编辑业务文件。
- StopFailure 只能记录失败上下文。

危险操作始终保留人工确认。
