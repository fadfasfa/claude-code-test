# 定期维护流程

本文说明 claudecode 仓库内两个需要人工定期执行的维护流程：

- `.tmp` 临时目录清理
- `.learnings/ERRORS.md` 到 `.learnings/LEARNINGS.md` 的经验精炼

这两个流程都应通过项目级 skill-style slash command 启动。入口位于 `.claude/skills/`，可在 Claude Code 交互 UI 中直接调用：

```text
/maintenance
/maintenance tmp
/maintenance learning
/promote-learning
/promote-learning plan <learning-id> <docs|skills|entry>
/promote-learning apply <learning-id> <docs|skills|entry>
/promote-learning apply selected queue
```

本仓只保留两个维护入口：`/maintenance` 负责只读盘点和 `ERRORS → LEARNINGS` 候选生成，`/promote-learning` 负责稳定 learning 的第二阶段晋升审查。它们不是 SessionStart 注入，未获明确授权时不会自动删除、写入或提交。

## 入口读取边界

`/maintenance` 和 `/promote-learning` 是窄范围只读维护入口。它们不应为了盘点而读取完整仓库入口链，也不应默认读取 `AGENTS.md`、`CLAUDE.md`、`PROJECT.md`、`work_area_registry.md` 或 `agent_tooling_baseline.md`。

`/maintenance tmp` 只读取 `.tmp/**`、`.gitignore` 和必要的 git ignore 状态。`/maintenance learning` 只读取 `.learnings/ERRORS.md`、`.learnings/LEARNINGS.md` 和 `.claude/tools/learning-loop/**` 只读工具。

`/promote-learning` 默认只读取 `.learnings/LEARNINGS.md`。只有目标层级明确是 `docs`、`skills` 或 `entry` 时，才分别小范围读取目标 docs、目标 `SKILL.md` 或 `AGENTS.md` / `CLAUDE.md` / `PROJECT.md`。

只读 smoke test 只验证入口可启动、参数分支和读取边界，不做危险失败试探，不故意传错 Read 参数，不主动运行预期会失败的 Bash。

## 1. `.tmp` 临时目录清理

`.tmp/` 是 ignored 运行态目录，不属于规则层，不应提交。

可保留：

- `.tmp/active-task/current.md`，如果当前仍有 active task
- 最近任务仍需参考的临时材料

可列为清理候选：

- `*backup*`
- `*merge*`
- `*.bak`
- `*.base`
- `*.ours`
- `*.theirs`

禁止：

- 不使用 `git clean`
- 不自动删除
- 不删除 worktree
- 不清理 `run/**`、`QuantProject/**`、`sm2-randomizer/**`

清理必须由用户确认具体路径后执行。

## 严格只读验收模式

当用户要求 strict read-only audit、只读验收或类似模式时：

- 不主动执行预期会失败的 Bash。
- 不做危险命令试探。
- 如果必须运行可能失败的命令，先说明 `PostToolUseFailure` hook 可能写入 `.learnings/ERRORS.md`，并获得用户确认。
- 这不改变现有 hook 逻辑，只是补充边界说明。

## 2. ERRORS → LEARNINGS 精炼

`.learnings/ERRORS.md` 是 ignored raw error cache，不是稳定规则层。

`.learnings/LEARNINGS.md` 用于保存已经确认有复用价值的经验。

`/maintenance learning` 已承接旧独立 learning promotion skill 的职责：从 `ERRORS.md` 分组提炼候选 learning，只输出候选和确认问题，不写入 `LEARNINGS.md`。

正确流向：

```text
一次错误
→ ERRORS raw cache
→ 定期提炼候选 learning
→ 用户确认
→ 写入 LEARNINGS
→ 多次复用后，另开任务晋升到 docs / skills / 入口规则
```

禁止：

- 不自动清空 `ERRORS.md`
- 不自动写 `LEARNINGS.md`
- 不把一次性错误晋升为规则
- 不自动修改 docs / skills / hooks / tools
- 不写入 global 或 kb

## 3. 维护频率

建议：

每周一次：

```text
/maintenance
```

大型重构后：

```text
/maintenance
```

只想清理 `.tmp` 候选：

```text
/maintenance tmp
```

只想精炼错误经验：

```text
/maintenance learning
```

## 4. 人工确认边界

`.tmp` 删除需要人工确认。

`LEARNINGS` 写入需要人工确认。

docs、skills、hooks、tools 的规则升级必须另开任务，不在 `/maintenance` 中自动执行。

## 5. LEARNINGS 晋升审查

`/promote-learning` 用于审查 `.learnings/LEARNINGS.md` 中已经多次复用、稳定的经验，判断是否应晋升到 `docs/**`、`.claude/skills/**` 或入口规则。

默认模式只输出候选和 patch plan：

```text
/promote-learning
/promote-learning plan <learning-id> <docs|skills|entry>
```

真正修改规则层必须由用户明确确认。确认可以是单条 apply 命令，也可以是在已有明确晋升队列上下文中的批量执行意图：

```text
/promote-learning apply <learning-id> <docs|skills|entry>
/promote-learning apply selected queue
```

selected promotion queue 由用户明确选择的一个或多个 learning 组成；用户说“执行”“应用”“按我要求处理”“确认处理这些”“执行上述队列”“应用这些晋升”“apply selected”“apply queue”时，可以一次性批量应用队列中的最小 patch，不需要逐条 learning 再输入 apply 命令。用户未明确选择队列时仍只输出候选；用户明确说“只生成 plan / 不修改文件”时绝对不得修改。

`/promote-learning` 不处理 `ERRORS.md` 到 `LEARNINGS.md` 的初次精炼；该步骤仍由 `/maintenance` 负责。

晋升边界：

- `LEARNINGS → docs`：适合已经多次复用、能形成稳定判断规则、适用于多个任务的经验。
- `docs → skills`：适合已经形成可重复操作流程、需要步骤或检查项配合的经验。
- `docs / skills → entry`：只适合高频、安全边界、路径边界、提交边界或语言可读性纪律。

禁止：

- 没有 selected promotion queue 和明确执行意图时不自动晋升
- 不把单次错误或具体业务细节提升为规则
- 不自动修改未选择的 docs / skills / hooks / tools / entry files
- 涉及 hooks / settings / git / 删除文件 / 跨仓修改时不得静默执行，必须说明风险和目标授权
- 不写入 global 或 kb
- 不提交
