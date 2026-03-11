# Hextech 工作流执行约束 V5.2

## 执行前必读
每次任务开始前，读取项目根目录的 agents.md 获取本次任务的：
- Branch_Name（必须在此分支操作）
- Target_Files（只能修改此范围内的文件）
- Verification_Command（执行完必须运行此命令）

## 范围控制
- 所有文件修改严格限定在 agents.md 的 Target_Files 范围内
- git add 禁止全量（禁止 git add .），只能 git add <具体文件路径>
- 禁止修改：.ai_workflow/ 下除 runtime_state.json 和 event_log.jsonl 以外的文件

## 分支隔离
- 所有修改必须在 agents.md 的 Branch_Name 指定分支内进行
- 禁止直接修改 main 分支
- 开始执行前先确认当前分支：git branch --show-current

## 执行完成后的必要动作（协议4）
1. 运行验证：.\run_verification.ps1 -Caller executor
2. 验证通过后更新状态：
   读取 .ai_workflow/runtime_state.json，将 execution_status 改为 done
3. 向终端输出：[STAGE: EXECUTION_DONE]
4. 停止，等待人类唤醒 Node C

## 事件日志
每完成一个 Step，向 .ai_workflow/event_log.jsonl 追加一条记录：
{"ts":"<时间戳>","step":<N>,"files_touched":[...],"change_reason":"...","risk_note":"...","test_impact":"..."}

## 语言规约
终端输出、注释、日志使用简体中文（代码、命令、报错堆栈除外）

## 零掩饰
禁止用 try...pass、@pytest.mark.skip、注释 assert、mock 被测函数等手段抑制错误
```

---

## 阶段四：Agent 启动与模型选择流程

**目标：建立每次任务的标准操作流程，等价于 VS Code 里 Claude Code 收到 [HANDOFF-WRITE] 后自动启动的效果**

Antigravity 没有 cc-switch 自动路由，需要手动按契约字段选模型。流程如下：

**收到 Gem 下发的 [HANDOFF-WRITE] 契约后：**

1. 在 VS Code（或 Antigravity 终端）执行契约写入，覆写 `agents.md`

2. 打开 Antigravity → Cockpit → 查看 `分配节点` 字段 → 按以下对应表选模型：

| agents.md 分配节点字段 | Antigravity 选哪个 Group | 选哪个模型 |
| :--- | :--- | :--- |
| 旗舰档 | Group 3 | Claude Opus 4.6 (Thinking) |
| 主力档 | Group 3 | Claude Sonnet 4.6 (Thinking) |
| 全局上下文档 | Group 1 | Gemini 3.1 Pro (High) |
| 异步档 | Group 1 | Gemini 3.1 Pro (Low) |
| 前端档 | Group 1 | Gemini 3.1 Pro (High/Low) |
| 轻量档 | Group 2 | Gemini 3 Flash |
| 备用档 | Group 3 | GPT-OSS 120B (Medium) |

3. 启动 Agent，首条消息固定格式：
```
请读取项目根目录的 agents.md 和 .agents/skills/hextech-workflow.md，
然后按 agents.md 的确定性执行清单开始执行，从协议0开始。
```

4. Agent 执行完成输出 `[STAGE: EXECUTION_DONE]` 后，你切换到 VS Code → Roo Code 执行审计。

---

## 阶段五：响应速度慢的工程补偿

**目标：缓解 Antigravity 比 VS Code Claude Code 慢的问题**

响应慢的根本原因是 Antigravity Agent 是多轮规划模式，每步都要思考再执行，而 VS Code Claude Code 是流式直接执行。三个补偿手段：

**补偿一：Skill 文件写得越具体，Agent 规划步骤越少**

`.agents/skills/hextech-workflow.md` 里把每个协议的动作写清楚，Agent 不需要自己想，直接执行，速度明显提升。上面阶段三给的模板已经尽量具体。

**补偿二：轻量任务直接用 Gemini 3 Flash**

单文件修复、注释补充、测试用例这类任务不需要 Thinking 模型，Flash 响应速度接近 Claude Code，用 Gemini Flash 跑轻量档任务体验会好很多。

**补偿三：Tools Offline 时的备用方案**

如果 Tools 偶发离线，不要等，直接切 VS Code Claude Code 手动执行当前 Step，执行完再切回 Antigravity 继续后续 Step。两个 IDE 操作同一个 git 仓库，中间状态完全共享，无缝衔接。

---

## 验收标准

完成以上五个阶段后，用一个真实的简单任务（推荐：单文件小修复）跑完完整流程验证：
```
Gem /START → /INTAKE → /ARCH-ON → /HANDOFF
→ Antigravity 写入 agents.md
→ Antigravity Agent 执行（选主力档 Sonnet 4.6）
→ 输出 [STAGE: EXECUTION_DONE]
→ VS Code Roo Code 审计
→ 合并四码块