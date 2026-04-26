---
description: 定期维护 .tmp 临时目录与 ERRORS 到 LEARNINGS 的精炼候选
argument-hint: "[tmp|learning]"
---

# 定期维护命令

你正在执行 claudecode 的定期维护流程。该命令只做盘点和候选生成，默认不删除、不写入、不提交。

## 范围

允许读取：
- `.tmp/**`
- `.learnings/ERRORS.md`
- `.learnings/LEARNINGS.md`
- `.claude/tools/learning-loop/**`
- `.gitignore`
- 当前仓库 Git 状态

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

## 参数

用户参数为：`$ARGUMENTS`

- 如果参数为空：同时执行 `.tmp` 盘点和 learning 候选精炼。
- 如果参数为 `tmp`：只执行 `.tmp` 盘点。
- 如果参数为 `learning`：只执行 learning 候选精炼。
- 如果参数不是上述值：停止并说明只支持 `tmp` / `learning` / 空参数。

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

执行只读检查：

1. 读取 `.learnings/ERRORS.md`。
2. 读取 `.learnings/LEARNINGS.md`。
3. 如果 `.claude/tools/learning-loop/check_learning_loop.py` 存在，可运行它做只读汇总。
4. 从 `ERRORS.md` 中提炼候选 learning，但不要写入。

候选 learning 格式：

```text
## 候选 L<N>：一句话标题

- 触发场景：
- 失败模式：
- 稳定结论：
- 后续规则：
- 不适用范围：
- 是否建议晋升：
```

晋升规则：

可以建议晋升：

- 同类错误重复出现
- 能形成可执行判断规则
- 对后续任务有复用价值
- 不是一次性环境故障
- 不会导致规则膨胀

不建议晋升：

- 单次偶发失败
- 已修复且低复现概率的路径错误
- 具体业务文件细节
- 临时网络/依赖波动
- 只适用于某一次任务的操作记录

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

- `.tmp` 清理：本命令只能输出清理计划。真正删除必须由用户另行明确确认具体路径。
- `LEARNINGS` 晋升：本命令只能输出候选 learning。真正写入 `.learnings/LEARNINGS.md` 必须由用户确认具体候选条目。
- 规则、skill、hook、tool 或 docs 改动：本命令只能提出建议，不能自动修改。
- 即使用户要求继续维护，也不能把本命令升级为自动 cleanup、auto-loop 或 commit 流程。

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
