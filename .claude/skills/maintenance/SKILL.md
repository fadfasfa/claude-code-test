---
name: maintenance
description: "定期维护 .tmp，并从 ERRORS 提炼 LEARNINGS 候选；用户手动通过 /maintenance 调用。"
disable-model-invocation: true
---

# maintenance

你正在执行 claudecode 的定期维护流程。该 skill 只做盘点和候选生成，默认不删除、不写入、不提交。

## 调用方式

用户手动通过 skill-style slash command 调用：

- `/maintenance`：同时执行 `.tmp` 盘点和 learning 候选精炼。
- `/maintenance tmp`：只执行 `.tmp` 盘点。
- `/maintenance learning`：只执行 `ERRORS → LEARNINGS` 候选精炼。

如果用户输入不是上述语义，停止并说明只支持空参数、`tmp` 或 `learning`。

## 范围

允许读取：

- `.tmp/**`
- `.learnings/ERRORS.md`
- `.learnings/LEARNINGS.md`
- `.claude/tools/learning-loop/**`
- `.gitignore`
- 当前仓库 Git 状态

读取范围规则：

- `/maintenance` 默认不读取 `AGENTS.md`、`CLAUDE.md`、`PROJECT.md`、`work_area_registry.md`、`agent_tooling_baseline.md`。
- `/maintenance tmp` 只读取 `.tmp/**`、`.gitignore` 和必要的 git ignore 状态。
- `/maintenance learning` 只读取 `.learnings/ERRORS.md`、`.learnings/LEARNINGS.md` 和 `.claude/tools/learning-loop/**` 只读工具。
- 不使用全文件大范围 Read；优先使用目录枚举、`rg`、`Select-String`、`Get-Content -TotalCount` 或分段读取。
- Read 因 pages、行号或工具参数失败时，不重复同类错误调用，改用 scoped 命令。

禁止处理：

- `run/**`
- `QuantProject/**`
- `sm2-randomizer/**`
- 全局 `C:\Users\apple\.claude/**`
- `C:\Users\apple\.codex/**`
- `C:\Users\apple\kb/**`

禁止动作：

- 不删除文件
- 不清空 `ERRORS.md`
- 不自动写入 `LEARNINGS.md`
- 不修改 `docs/**`、`.claude/skills/**`、hooks、tools
- 不 `git clean`
- 不 `git reset`
- 不 `git stash`
- 不 `git add .`
- 不 commit
- 不 push

## 严格只读验收模式

当用户要求 strict read-only audit、只读验收或类似模式时：

- 不主动执行预期会失败的 Bash。
- 不做危险命令试探。
- 如果必须运行可能失败的命令，先说明 `PostToolUseFailure` hook 可能写入 `.learnings/ERRORS.md`，并获得用户确认。
- 这不改变现有 hook 逻辑，只是补充边界说明。

## Markdown / Text 读取 fallback

读取 Markdown / text 文件时，不传 PDF page 参数。避免一次性读取超大范围。

若 Read 因空 PDF page、行号范围或工具参数失败，应改用 scoped `Get-Content`、`Select-String` 或 `rg` 进行小范围读取。只读验收优先搜索和分段读取，不做全文件暴力读取。

## A. `.tmp` 盘点流程

执行只读检查：

1. 查看 `.tmp` 是否被 ignore。
2. 列出 `.tmp` 下一级目录。
3. 识别：
   - `active-task/current.md`
   - `*backup*`
   - `*merge*`
   - `*.bak`
   - `*.base`
   - `*.ours`
   - `*.theirs`
4. 输出清理候选，但不要删除。

判断规则：

- `.tmp/active-task/current.md` 如果存在且任务未结束，应保留。
- `backup`、`merge`、`.bak`、`.base`、`.ours`、`.theirs` 通常是清理候选。
- 如果发现像规则草稿、learning 候选或未迁移材料，不能列为直接删除，必须单独提示用户确认。
- 不允许自动删除。

输出格式：

```text
## .tmp 盘点

- .tmp 是否 ignored：
- 是否存在 active-task/current.md：
- 应保留：
- 可清理候选：
- 不应自动清理：
- 需要用户确认的问题：
```

## B. ERRORS → LEARNINGS 精炼流程

`/maintenance learning` 承接旧独立 learning promotion skill 的职责：只把 ignored raw error cache 精炼为 tracked learning 候选，不负责 `LEARNINGS → docs / skills / entry` 的第二阶段晋升审查。

执行只读检查：

1. 读取 `.learnings/ERRORS.md`。
2. 读取 `.learnings/LEARNINGS.md`。
3. 如果 `.claude/tools/learning-loop/check_learning_loop.py` 存在，可运行它做只读汇总。
4. 从 `ERRORS.md` 中提炼候选 learning，但不要写入。
5. 不把 `.tmp/active-task/current.md` 或其他 runtime ledger 当作 learning 来源。

错误分组方式：

- 按 source、触发场景、工具/命令、失败签名和可复现性分组。
- 标记每组是 repeated 还是 one-off。
- 标记是否有 repo-local 复用价值。
- 标记是否存在过度泛化风险。
- 已被现有 `LEARNINGS.md` 覆盖的内容列为重复，不再生成新候选。

候选 learning 格式：

```text
## 候选 L<N>：一句话标题

- 来源：
- 分组判断：repeated / one-off
- 触发场景：
- 失败模式：
- 稳定结论：
- 后续规则：
- repo-local 复用价值：
- 拟写入文本：
- 过度泛化风险：
- 不适用范围：
- 是否建议写入 LEARNINGS：
```

建议写入 LEARNINGS：

可以建议写入：

- 同类错误重复出现
- 能形成可执行判断规则
- 对后续任务有复用价值
- 不是一次性环境故障
- 不会导致规则膨胀
- 不是具体业务文件细节
- 不与现有 `LEARNINGS.md` 重复

不建议写入：

- 单次偶发失败
- 已修复且低复现概率的路径错误
- 具体业务文件细节
- 临时网络/依赖波动
- 只适用于某一次任务的操作记录
- 已被 `LEARNINGS.md` 或现有规则覆盖的内容
- 风险是把一次任务上下文过度泛化为长期规则

输出格式：

```text
## ERRORS → LEARNINGS 候选

- ERRORS 是否存在：
- LEARNINGS 是否存在：
- 原始错误分组：
- 候选 learning：
- 不建议晋升的内容：
- 需要用户确认的问题：
```

## 二次确认规则

- `.tmp` 清理：本 skill 只能输出清理计划。真正删除必须由用户另行明确确认具体路径。
- `LEARNINGS` 晋升：本 skill 只能输出候选 learning。真正写入 `.learnings/LEARNINGS.md` 必须由用户确认具体候选条目。
- 规则、skill、hook、tool 或 docs 改动：本 skill 只能提出建议，不能自动修改。
- 即使用户要求继续维护，也不能把本 skill 升级为自动 cleanup、auto-loop 或 commit 流程。
- 高阶晋升：`LEARNINGS → docs / skills / entry` 由 `/promote-learning` 处理，不在本 skill 中执行。

## 收尾

最后只输出：

```text
## 维护结果

- 本次模式：
- 是否做了任何修改：否
- .tmp 清理候选：
- learning 晋升候选：
- 需要用户确认的下一步：
```

不要执行删除、写入、stage、commit 或 push。
