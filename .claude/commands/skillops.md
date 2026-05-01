---
description: Unified skill maintenance command. Audit, optimize, and promote Claude Code skill improvements with controlled automation.
argument-hint: "[audit|optimize <skill>|promote|weekly|apply-safe|history]"
allowed-tools: Read, Glob, Grep, Bash(git status:*), Bash(git diff:*), Bash(git ls-files:*), Bash(powershell:*), Bash(pwsh:*)
---

# /skillops

你是 Claude Code skill 维护入口。根据 `$ARGUMENTS` 路由到对应模式。

## 总原则

- 默认只读。
- 不恢复 `.ai_workflow`。
- 不创建 lowercase `agents.md`。
- 不恢复 `.agents/skills`。
- 不恢复 cc-switch。
- 不修改 `run/**`。
- 不自动修改 hooks/settings/permissions。
- 不自动 commit / push / revert。
- `planning-with-files` 只作为规划辅助，不是状态机。
- Web AI 是参考层，不是任务派发必经环节。
- Codex 是全局/跨仓库/云审查，不默认接管本仓本地实现。

## 正式 skill 来源

只检查：

- `C:\Users\apple\.claude\skills`
- `C:\Users\apple\claudecode\.claude\skills`

冻结来源：

- `C:\Users\apple\.agents\skills`

冻结来源只允许确认其保持冻结，不允许恢复。

## 路由

### 无参数或 audit

执行只读审计：

1. 检查正式 skill 来源。
2. 检查每个 `SKILL.md` 第一行是否为 `---`。
3. 检查 `name` / `description`。
4. 检查 description 是否过宽。
5. 检查 repo-specific 内容是否误放到 global skill。
6. 检查 `.agents/skills` 是否仍冻结。
7. 检查 cc-switch 是否无引用。
8. 检查 hooks 是否没有 Read/Edit/Write matcher。
9. 输出报告，不修改文件。

### optimize <skill>

执行受控 Darwin 优化：

1. 只处理指定 skill。
2. 先读取目标 `SKILL.md`。
3. 设计 2-3 个测试 prompts。
4. 生成 baseline 评分。
5. 只生成候选修改和 diff。
6. 候选产物写入临时目录，不直接覆盖正式 `SKILL.md`。
7. 输出是否建议 apply。
8. 等用户确认后才允许写入正式 skill。

禁止：

- 批量优化；
- 自动 commit；
- 自动 revert；
- 修改 hooks/settings/permissions；
- 修改 global skills，除非用户明确指定。

### promote

从最近一次真实问题或用户提供的经验中提炼候选规则：

1. 判断应写入哪里：
   - 当前回复临时建议；
   - repo `CLAUDE.md`；
   - repo `AGENTS.md`；
   - 某个 repo-local `SKILL.md`；
   - global skill；
   - 不应写入。
2. 输出候选 patch。
3. 不自动落盘，除非用户明确确认。

### weekly

执行周期性只读巡检：

1. 检查 skills 格式。
2. 检查冻结源。
3. 检查 cc-switch 复活迹象。
4. 检查 hooks 漂移。
5. 检查 Read `pages:""` known issue 是否仍记录。
6. 检查是否有 skill description 过宽。
7. 输出维护报告。

### apply-safe

只允许低风险机械修复，必须先列清单并等待确认：

- 修 frontmatter 第一行；
- 补缺失 name/description；
- 修 name 格式；
- 移动明显临时文件到草稿目录。

### history

读取维护记录和优化记录，输出摘要。

## 输出格式

输出中文报告：

- 模式；
- 检查范围；
- 发现的问题；
- 风险等级；
- 建议动作；
- 是否需要用户确认；
- 是否修改文件。
