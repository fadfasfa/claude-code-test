<!-- claudecode-repo-local -->
---
name: self-improvement-promotion
description: Promote claudecode repo-local learning candidates from raw logs into tracked learning only after user review.
---

# self-improvement-promotion

中文简介：本 skill 用于把 claudecode repo-local raw logs 中的候选经验晋升为 tracked learning。它只负责人工审查后的 repo-local learning；不负责自动 global learning、自动更新 `kb` 或提交 raw logs。

## 什么时候使用

用户要求 review 或 promote repo-local learnings 时使用。

## 来源

- Raw error cache：`.learnings/ERRORS.md`
- Tracked repo learning：`.learnings/LEARNINGS.md`
- 相关 repo-local skills/hooks/tools/docs

## 规则

- Raw logs 是 ignored inputs，不是 git content。
- 不自动晋升 learnings。
- 不从本仓工作流写 global learning。
- 不修改 `kb`。
- 不把 runtime ledger entries 和 learning 混在一起。
- 只晋升稳定、可复用、repo-local 的经验。

## 晋升清单

每个候选项都报告：

- source
- repeated or one-off
- repo-local value
- proposed target section
- exact wording
- risk of overgeneralization
- whether user confirmation is required

## 写入边界

只有用户确认候选清单后，才更新 `.learnings/LEARNINGS.md`。
