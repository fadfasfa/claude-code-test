---
name: promote-learning
description: "审查 LEARNINGS 是否晋升到 docs、skills 或入口规则；用户手动通过 /promote-learning 调用。"
disable-model-invocation: true
---

# promote-learning

你正在执行 claudecode 的 learning 晋升审查流程。

该 skill 用于判断 `.learnings/LEARNINGS.md` 中的稳定经验是否应晋升到：

- `docs/**`
- `.claude/skills/**`
- `AGENTS.md`
- `CLAUDE.md`
- `PROJECT.md`
- `README.md`

默认只输出晋升建议和 patch plan，不直接修改规则层。

## 调用方式

用户手动通过 skill-style slash command 调用：

- `/promote-learning`：审查所有 LEARNINGS，输出晋升候选。
- `/promote-learning plan <learning-id> <docs|skills|entry>`：为指定 learning 输出精确 patch plan。
- `/promote-learning apply <learning-id> <docs|skills|entry>`：只有当用户明确输入 apply，且当前任务授权允许写入时，才允许执行最小修改。

如果参数不清楚，停止并要求用户指定 learning-id 和目标层级。

## 允许读取

- `.learnings/LEARNINGS.md`
- `.learnings/ERRORS.md`
- `docs/**`
- `.claude/skills/**/SKILL.md`
- `AGENTS.md`
- `CLAUDE.md`
- `PROJECT.md`
- `README.md`
- `agent_tooling_baseline.md`

读取 Markdown / text 文件时，不传 PDF page 参数。避免一次性读取超大范围。若 Read 因空 PDF page、行号范围或工具参数失败，应改用 scoped `Get-Content`、`Select-String` 或 `rg` 进行小范围读取；只读验收优先搜索和分段读取，不做全文件暴力读取。

## 禁止读取或处理

- `run/**`
- `QuantProject/**`
- `sm2-randomizer/**`
- 全局 `C:\Users\apple\.claude/**`
- `C:\Users\apple\.codex/**`
- `C:\Users\apple\kb/**`

## 禁止动作

- 不自动晋升
- 不把单次错误晋升为规则
- 不把具体业务细节晋升为通用规则
- 不修改 hooks / tools，除非用户明确指定
- 不删除 ERRORS
- 不 git clean
- 不 git reset
- 不 git stash
- 不 git add .
- 不 commit
- 不 push

## 晋升层级

### 1. ERRORS → LEARNINGS

由 `/maintenance` 处理。这里不负责。

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

`apply` 只在用户本轮明确要求 `/promote-learning apply <learning-id> <docs|skills|entry>` 时允许执行。

执行时仍必须保持最小修改：

- `docs`：只更新相关 `docs/*.md`。
- `skills`：只更新用户确认的 `.claude/skills/**/SKILL.md`。
- `entry`：只更新用户确认的 `AGENTS.md`、`CLAUDE.md`、`PROJECT.md` 或 `README.md`。

不得顺手迁移其他 learning，不得清空 `ERRORS.md`，不得提交。
