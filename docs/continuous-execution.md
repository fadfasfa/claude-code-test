# 连续执行治理

本文件说明 Claude Code 在计划被确认后如何持续推进到明确终点，同时保留安全边界。

## 运行态 Ledger

路径：

```text
.tmp/active-task/current.md
```

这个 ledger 是 ignored 运行态记录。它不是规则层，不是 learning，不是提交产物，也不是授权凭证。

当前 repo 任务不得把 `C:\Users\apple\.claude\plans\*.md` 当作必须写入的运行态文件。计划审批通过 ExitPlanMode 或对话提交完成，不强制落盘到全局 plans。

如确实需要计划落盘，只能写当前 repo 内的 `.tmp/active-task/current.md`。写入全局 `.claude\plans` 必须由用户显式要求，且不能用 PowerShell `Set-Content` 作为默认 fallback。

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

- 范围变化；但同一业务闭环内的直接依赖 helper 文件不算业务目标扩大，前提是每个新增目标文件都说明原因，并满足 scripted patch 唯一匹配约束。
- 目标工作区不清楚。
- dirty tree 在同一文件里混有无关用户改动。
- 需要安装、卸载或升级依赖。
- 需要修改全局层或 `kb`。
- 需要新增 Playwright config/script/hook/tool，但还没有模块准入卡。
- git 操作是 `push`、PR、`merge`、`reset`、`clean`、`rebase`、`stash` 或 `worktree remove`。
- `git add` / `git commit` 缺少计划授权、清晰 diff 范围或已确认 message。
- 新增目标文件超过 3 个、跨到新模块、删除文件、改变业务目标或涉及破坏性数据操作。

## 严格只读验收模式

当用户要求 strict read-only audit、只读验收或类似模式时：

- 不主动执行预期会失败的 Bash 命令。
- 不做危险命令试探，包括删除、清理、reset、stash、force remove、安装或写全局层的 dry-run 伪验证。
- 只读 smoke test 只验证可启动和边界，不通过故意失败的命令或错误参数试探 hook。
- 如果必须运行可能失败的命令，先说明 `PostToolUseFailure` hook 可能把失败记录写入 `.learnings/ERRORS.md`，并获得用户确认。
- 这只补充执行边界说明，不改变现有 hook 逻辑。

## Native Read ban for text/code files

当前本仓已知 Claude Code 原生 `Read` 对 text/code 文件可能自动携带 `pages` 参数并失败。因此连续执行时也必须遵守：

- 不要在主线程或 subagent 中对 `.md`、`.txt`、`.json`、`.py`、`.ps1`、`.html`、`.css`、`.js`、`.ts`、`.tsx`、`.jsx`、`.yaml`、`.yml`、`.toml`、`.csv` 文件使用原生 `Read`。
- 源码和文档探索优先使用 `.claude/agents/repo-explorer.md`，或直接用 `Grep` / `Glob` / `Bash` 获取少量片段。
- `repo-explorer` 只能使用 `Grep` / `Glob` / `Bash`。
- 如果主线程或 subagent 已经发生一次 `Read` `pages` / unsupported parameter / malformed input 失败，不得重试同文件 `Read`。
- 不得声称“这次不传 pages”后再次发起同类 `Read`。
- 需要上下文时，用 `Grep` / `Glob` / `Bash` 获取片段或让 `repo-explorer` 汇总。
- 如果仍需要完整上下文，停止并报告 blocker；不得发明脚本读取或写入绕过。
- `full_synergy_scraper.py` 这类源码文件不能“重试读取”；首次遇到 `Read` 参数失败后，同文件原生 `Read` 路径立即关闭。

## Edit safety after missing Read registration

如果 `Edit` / `Write` 因没有成功原生 `Read` 登记而失败：

- 未授权业务闭环时，不使用 PowerShell `Set-Content`、`[System.IO.File]::WriteAllText` 或 ad-hoc replacement scripts 修改业务文件；停止并报告 blocker，等待逐文件授权。
- 已授权业务闭环时，scoped scripted patch fallback 不视为额外权限升级；可在同一闭环内修改直接依赖 helper 文件，但每个新增目标文件都必须说明原因。
- Before any scripted patch, confirm the exact target string or block matches once, print the match count, and stop if the count is not `1`.
- 不得借 fallback 修改 hooks、settings、permissions、skills、`.git`、`.claude.json`、全局配置、`.ai_workflow` 或 lowercase `agents.md`。
- 若新增目标文件超过 3 个、跨到新模块、删除文件、改变业务目标或涉及破坏性数据操作，必须先停下确认。

## Retry budget

- Native `Read` budget for text/code files is zero in the main thread and subagents.
- If a `Read` `pages` / unsupported parameter / malformed input failure has already happened, do not retry native `Read` for the same file.
- Use built-in `Grep` / `Glob` / `Bash` or scoped PowerShell/Python read-only fallback inside the already authorized business loop.

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
