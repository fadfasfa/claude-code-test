# 定期维护流程

本文说明 claudecode 仓库内两个需要人工定期执行的维护流程：

- `.tmp` 临时目录清理
- `.learnings/ERRORS.md` 到 `.learnings/LEARNINGS.md` 的经验精炼

这两个流程都应通过项目级 slash command 启动：

```text
/maintenance
/maintenance tmp
/maintenance learning
```

Slash commands 更适合经常重复的一段提示或固定流程入口；skills 更适合复杂能力、脚本和多文件知识组织。这个定期维护入口属于固定触发流程，所以使用 `/maintenance`，不新增 hook，也不强塞 skill。

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

## 2. ERRORS → LEARNINGS 精炼

`.learnings/ERRORS.md` 是 ignored raw error cache，不是稳定规则层。

`.learnings/LEARNINGS.md` 用于保存已经确认有复用价值的经验。

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
