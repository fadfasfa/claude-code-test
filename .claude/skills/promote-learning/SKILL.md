---
name: promote-learning
description: "审查稳定 LEARNINGS 是否晋升到 docs、skills 或入口规则；用户手动通过 /promote-learning 调用。"
disable-model-invocation: true
---

# promote-learning

你正在执行 claudecode 的 learning 晋升审查流程。

该 skill 只用于判断 `.learnings/LEARNINGS.md` 中的稳定经验是否应晋升到：

- `docs/**`
- `.claude/skills/**`
- `AGENTS.md`
- `CLAUDE.md`
- `PROJECT.md`

默认只输出晋升建议和 patch plan，不直接修改规则层。

## 调用方式

用户手动通过 skill-style slash command 调用：

- `/promote-learning`：审查所有 LEARNINGS，输出晋升候选。
- `/promote-learning plan <learning-id> <docs|skills|entry>`：为指定 learning 输出精确 patch plan。
- `/promote-learning apply <learning-id> <docs|skills|entry>`：单条 apply 入口；当用户明确输入 apply，且当前任务授权允许写入时，执行最小修改。
- `/promote-learning apply selected queue`：batch apply 入口；当用户已经明确选择一个或多个晋升项，并给出执行意图时，按已选 promotion queue 批量执行最小修改；不要求每条 learning 再单独输入 apply 命令。

如果参数不清楚，且当前对话里没有明确的 selected promotion queue，停止并要求用户指定 learning-id 和目标层级。

## 读取范围

默认模式只读取：

- `.learnings/LEARNINGS.md`

`plan` / `apply` 模式只读取目标层级相关文件的小范围内容：

- `docs`：只有目标层级是 `docs` 时，才读取相关目标 `docs/*.md` 的候选位置和少量上下文。
- `skills`：只有目标层级是 `skills` 时，才读取相关目标 `.claude/skills/**/SKILL.md` 的候选位置和少量上下文。
- `entry`：只有目标层级是 `entry` 时，才小范围读取 `AGENTS.md`、`CLAUDE.md` 或 `PROJECT.md` 的候选位置和少量上下文。

读取规则：

- 默认只读取 `.learnings/LEARNINGS.md`。
- 不默认读取完整入口链，包括 `AGENTS.md`、`CLAUDE.md`、`PROJECT.md`、`work_area_registry.md` 和 `agent_tooling_baseline.md`。
- 不读取 `AGENTS.md`、`CLAUDE.md` 或 `PROJECT.md`，除非目标层级明确是 `entry`。
- 不默认读取 `.learnings/ERRORS.md`，也不读取 `ERRORS.md` 做初次提炼；只有用户明确要求核对来源时才可小范围读取。
- 不处理 `ERRORS.md` 到 `LEARNINGS.md` 的初次提炼；该步骤由 `/maintenance learning` 处理。
- 不使用全文件大范围 Read；优先使用 `rg`、`Select-String`、`Get-Content -TotalCount` 或小范围分段读取。
- 读取 Markdown / text 文件时，不传 PDF `pages` 参数，不传空 `pages`，也不混用 PDF 专用参数。
- 若 Read 因空 PDF page、行号范围或工具参数失败，失败一次后禁止用相同参数重试，应改用 scoped `Get-Content`、`Select-String` 或 `rg` 进行小范围读取。

## 禁止读取或处理

- `run/**`
- `QuantProject/**`
- `sm2-randomizer/**`
- 全局 `C:\Users\apple\.claude/**`
- `C:\Users\apple\.codex/**`
- `C:\Users\apple\kb/**`

## 禁止动作

- 不自动晋升
- 不从 `ERRORS.md` 提炼初次 learning
- 不把单次错误晋升为规则
- 不把具体业务细节晋升为通用规则
- 不修改 hooks / tools，除非用户明确指定
- 不在没有 selected promotion queue 和明确执行意图时自动晋升
- 不删除 ERRORS
- 不 git clean
- 不 git reset
- 不 git stash
- 不 git add .
- 不 commit
- 不 push

## 晋升层级

### 1. ERRORS → LEARNINGS 不在本 skill 处理

由 `/maintenance learning` 处理。这里不读取 `ERRORS.md` 生成初次候选。

### 2. LEARNINGS → docs

适合条件：

- 同一经验已经多次复用
- 能形成稳定判断规则
- 适用于多个任务
- 不依赖具体业务文件
- 不会明显增加流程负担

### 3. docs → skills

适合条件：

- 已经形成可重复操作流程
- 需要步骤、检查项或脚本配合
- 不适合每次塞进入口文件
- 适合按需调用

### 4. docs / skills → 入口规则

适合条件：

- 高频发生
- 影响所有 agent 行为
- 是安全边界、路径边界、提交边界或语言可读性纪律
- 不写入入口会导致反复偏移

入口规则包括：

- `AGENTS.md`
- `CLAUDE.md`
- `PROJECT.md`

## 不应晋升

以下内容不应晋升：

- 一次性环境故障
- 某个业务文件的临时问题
- 已经通过局部 patch 解决且低复现的问题
- 过度保守、会显著拖慢小任务的规则
- 与现有规则重复的表述
- 只适合当前任务上下文的记录

## 输出格式

默认模式输出：

```text
## Learning 晋升审查

- LEARNINGS 总数：
- 可晋升候选：
- selected promotion queue 候选：
- 不建议晋升：
- 重复或已被覆盖：
- 需要用户确认的问题：
```

`plan` 模式输出：

```text
## Learning 晋升 patch plan

- learning-id：
- 目标层级：
- 目标文件：
- 建议插入位置：
- 建议文本：
- 为什么不是更高或更低层级：
- 风险：
- 需要用户确认的问题：
```

`apply` 模式输出：

```text
## Learning 晋升 apply 结果

- learning-id：
- 目标层级：
- 修改文件：
- 修改摘要：
- 验证结果：
- 未执行动作：
```

## Apply 边界

`apply` 允许在以下两种授权下执行：

1. 用户本轮明确要求 `/promote-learning apply <learning-id> <docs|skills|entry>`。
2. 用户已经明确选择一个或多个晋升项形成 selected promotion queue，并给出明确执行意图，例如“执行”“应用”“按我要求处理”“确认处理这些”“执行上述队列”“应用这些晋升”“apply selected”“apply queue”。

selected promotion queue 必须能从当前对话或刚输出的审查结果中确定 learning-id、目标层级和排除项；用户标记“暂缓”“不处理”“只生成 plan”的 learning 不得进入本批次。用户明确说“只生成 plan / 不修改文件”时，绝对不得 apply。

执行时仍必须保持最小修改：

- `docs`：低风险 docs 晋升可在 selected queue 授权下自动应用最小 patch，只更新相关 `docs/*.md`。
- `skills`：只更新 selected queue 中用户确认的 `.claude/skills/**/SKILL.md`；涉及风险时先说明风险和目标文件，但不要机械要求逐条 learning 单独 apply。
- `entry`：可在同一批次中更新 selected queue 指定的 `AGENTS.md`、`CLAUDE.md` 或 `PROJECT.md`，但必须保持最小补丁，不新增大段规则。
- `hooks` / `settings` / `git` / 删除文件 / 跨仓或 global / `kb` 写入：不得静默执行；必须单独说明风险、目标和所需授权。即使需要额外授权，也不要退回到逐条 learning apply 命令。

不得顺手迁移未选择的 learning，不得清空 `ERRORS.md`，不得提交。
