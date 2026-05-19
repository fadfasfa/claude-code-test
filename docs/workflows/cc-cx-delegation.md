# CC / CX Delegation Workflow

本文件定义当前 `claudecode` 仓库的 CC-CX 硬分工。目标是把探查、计划证据收集、代码定位、patch 生成、apply 和最小验证全部交给 Codex，让 CC 只保留监督和审查职责。

## Roles

- CC Orchestrator
  - 负责需求澄清、任务派发、计划审批、结果审查。
  - NORMAL 状态下直接写入仅限 `.claude/plans/**` 和 `.state/cc-work/**`。
  - 可直接读取 `CLAUDE.md`、`AGENTS.md`、`PROJECT.md` 和 `docs/workflows/**` 以完成控制面审查。
  - 不直接探查、执行或修改业务 protected path，除非进入 CC break-glass 状态。
- Codex Researcher
  - 负责只读探查、代码定位、数据流梳理、证据收集。
  - 允许读取 protected path，但不得修改文件。
- Codex Planner
  - 负责把探查结果收敛为可审批计划。
  - 计划必须显式标注目标路径、预期 patch 面、验证入口、风险和停止条件。
- Codex Executor
  - 负责在 plan approved 后生成 patch、修改文件并运行最小验证。
  - 必须输出 changed files、diff summary、validation result。
- Codex Reviewer
  - 负责复核执行结果、失败状态、部分修改和回滚建议。

同一次 Codex 会话可以承担多个角色，但每个阶段的约束都必须保留。

## Entry Contract

- CC 调用 CX 的默认主入口是已启用的 OpenAI 官方 Codex plugin。
- `cx-exec.ps1` 如在历史路径、缓存或兼容层出现，只能作为 legacy/compat，不得作为 CC-CX 主流程要求。
- plugin 启用不等于 review gate 启用；review gate 默认禁用，除非用户显性要求，否则不得启用。
- Codex standalone 能力保留；用户直接调用 Codex 时不经过 CC-CX 委派层。

## Phases

1. Intake
   - 由 CC Orchestrator 澄清目标、范围、约束和审批门槛。
   - CC 不直接读取 protected path。
2. Explore
   - 由 Codex Researcher 只读探查。
   - 产出关键文件、行号、调用链、验证入口、风险与不确定项。
3. Plan
   - 由 Codex Planner 生成计划，CC Orchestrator 整理审批面。
   - 未获批准前不得修改文件。
4. Apply
   - 由 Codex Executor 按 approved plan 修改。
   - 优先生成 unified diff；不得凭猜测 patch。
5. Verify
   - 由 Codex Executor 运行最小有效验证。
   - 若验证失败，必须报告失败点、影响文件和回滚建议。
6. Review
   - CC Orchestrator 只读审查 Codex 报告和 `git diff` 摘要。
   - 审查不等于直接下场读写 protected path。
7. Commit
   - 仅在用户明确授权后执行。

## Explore Hard Rules

- Codex Researcher 可以读取 protected path。
- CC 不直接 `Read` / `Glob` / `Grep` / `LS` protected path，也不通过 Bash 进行等价探查。
- Researcher 阶段禁止：
  - `Edit`
  - `Write`
  - `MultiEdit`
  - `apply_patch`
  - shell 写文件
  - `rm` / `mv` / `cp` / `sed -i` / `tee` / `Out-File` / `Set-Content`
  - 启动长时间服务
  - 修改 Guard
- Researcher 输出必须包含：
  - 关键文件和行号
  - 数据流或调用链
  - 证据
  - 不确定项
  - 建议的下一步
- Researcher 不得直接提出 patch，除非已经进入 Plan 阶段。

## Apply Hard Rules

- Executor 只能在 plan approved 后运行。
- Executor 优先生成 unified diff。
- 不允许凭猜测 patch。
- `old_string` 不匹配时必须停止并报告，不得反复猜测。
- 连续 3 次 shell failure 必须停止。
- 连续 3 次 shell failure 后不得继续猜测，不得换壳重试，不得仅通过改写 shell 包装继续硬闯。
- 超过 5 分钟无有效进展必须停止。
- 失败后必须明确说明：
  - 已完成步骤
  - 未完成步骤
  - 最后失败点
  - 是否有文件被修改
  - 如有，是否已回滚
  - 若未回滚，建议的回滚动作是什么
- 修改后必须输出：
  - changed files
  - diff summary
  - validation result

## Protected Path

当前默认 protected path 包括：

- `run/**`
- `QuantProject/**`
- `heybox/**`
- `qm-run-demo/**`
- `sm2-randomizer/**`
- `subtitle_extractor/**`
- `AGENTS.md`
- `CLAUDE.md`
- `PROJECT.md`
- `README.md`
- `docs/workflows/**`
- `.claude/**`，但 `.claude/plans/**` 为 CC 草稿例外
- `.agents/skills/**`

`.state/cc-work/**` 与 `.claude/plans/**` 是 CC 草稿面，不属于业务修改面。`CLAUDE.md`、`AGENTS.md`、`PROJECT.md` 和 `docs/workflows/**` 允许 CC 在 NORMAL 状态下直接只读审查；写入仍属于 protected path。

## Guard State Machine

Guard 状态源优先为 `.state/cc-work/cc-cx-state.json`。文件缺失时状态为 `NORMAL`。测试或临时运行可以用 `CC_CX_STATE_PATH` 指向临时状态文件；这不改变仓库默认状态源。

状态文件格式：

```json
{
  "state": "NORMAL",
  "reason": "optional human-readable reason",
  "authorized_by": "user",
  "approved_plan_id": "plan-id-for-bg-write",
  "approved_files": [
    "relative/path/to/approved-file.py"
  ],
  "git": {
    "add": false,
    "commit": false,
    "push": false
  }
}
```

- `NORMAL`：CX-first 默认状态。CC 不直接访问业务 protected path；Codex plugin control-plane 可启动、查询、中断和恢复任务。
- `CX_DEGRADED`：官方 Codex Plugin 无法启动、Codex 线程启动失败、线程不能执行本地命令、只读 smoke test 失败、同阶段连续失败或卡死无进展时使用。该状态下不得继续反复启动或恢复 Codex 执行线程，只允许 status/cancel/report 类收敛动作。
- `CC_BG_READ`：用户授权后本会话持续有效。CC 可直接 `Read` / `Grep` / `Glob` / `LS` protected path；不得 `Edit` / `Write` / `MultiEdit`，也不得通过 Bash 探查 protected path。
- `CC_BG_WRITE`：必须每个 approved plan 单独授权。CC 只能 `Edit` / `Write` / `MultiEdit` 状态文件 `approved_files` 精确列出的文件；非 approved files、删除、移动、批量格式化和 Bash 写入继续拒绝。
- Git 授权独立于状态：`git add`、`git commit`、`git push` 默认拒绝；`push` 必须单独授权，不能继承 `commit` 授权。

deny 输出必须包含结构化字段：

```json
{
  "rule_id": "PROTECTED_READ_DENIED",
  "state": "NORMAL",
  "tool_name": "Read",
  "matched_path": "run/foo.py",
  "reason": "human-readable reason"
}
```

## Codex Control-Plane Allowlist

Guard 显式允许以下 control-plane 命令，不会因为 prompt 文本中包含 `run/**`、`QuantProject/**`、`>`、`<task>` 或 `--write` 就误判为直接访问：

- `node .../codex-companion.mjs task ...`
- `node .../codex-companion.mjs status`
- `node .../codex-companion.mjs cancel ...`
- `node .../codex-companion.mjs resume ...`
- `node .../codex-companion.mjs task-resume-candidate ...`
- `codex task ...`
- `codex cancel ...`
- `codex resume ...`
- `codex status`
- `codex review`

allowlist 只放行这些明确命令，不等于“任何包含 codex 字符串的 Bash 都放行”。

路径判断必须基于 `tool_name + tool_input` 中的语义化路径字段：`Grep.pattern`、prompt、description 和自由文本说明不是 path target，不得单独触发 protected-path deny。JSON parse fallback 不得粗暴扫描 raw prompt；无法安全解析时按结构化 `JSON_PARSE_FAILED` 拒绝。

## Codex Data-Plane Readonly Allowlist

Codex control-plane 放行只表示 CC 可以启动、查询或中断 Codex 任务；它不自动放行 Codex 线程内部的 Bash。Guard 只在 hook payload 明确标识 Codex Researcher data-plane 时，才允许 protected path 的只读探查。

companion 必须向 hook payload 注入以下 metadata：

```json
{
  "codex_delegation": {
    "source": "codex-thread",
    "role": "researcher",
    "phase": "explore"
  }
}
```

如果 companion 只能注入环境变量，也必须把等价字段暴露进 hook payload 的 `env` 对象，而不是写成全局 CC 环境变量：

- `CODEX_DELEGATION_SOURCE=codex-thread`
- `CODEX_DELEGATION_ROLE=researcher`
- `CODEX_DELEGATION_PHASE=explore`

Guard 的 data-plane allow 条件必须同时满足：

- 来源明确为 Codex 线程：`source=codex-thread` 或 `source=openai-codex-thread`。
- `role=researcher` 或 `phase=explore`，且已出现字段不得与 researcher/explore 冲突。
- Bash 命令命中只读 allowlist。
- Bash 命令不包含写信号或高危 Git 写操作。

只读 allowlist 当前覆盖：

- `Get-Content` / `gc` / `type` / `cat`
- `cmd /c type`
- `findstr`
- `Select-String` / `sls`
- `rg`
- `grep`
- `Get-ChildItem` / `gci`
- `dir`
- `ls`
- `git status`
- `git diff`
- `git log`
- `git ls-files`

以下信号即使来自 Codex Researcher 也必须拒绝：

- `>` / `>>`
- `Out-File` / `Set-Content` / `Add-Content` / `tee`
- `rm` / `del` / `Remove-Item`
- `mv` / `move` / `cp` / `copy`
- `node fs.writeFileSync`
- `python open(..., 'w')`
- `git rm`
- `git checkout --`
- `git reset`
- `git clean`

缺少上述 metadata 时，Codex data-plane Bash 会继续按 CC 直接调用处理；这会阻止读取 `run/**` 等 protected path。

## Guard Governance Rule

- `.claude/settings.json` 与 `.claude/hooks/cc-delegation-guard.ps1` 属于 Guard 治理面。
- 业务修复任务不得修改 Guard。
- 如需修改 Guard，任务标题和计划中必须明确声明这是治理任务。
- Guard 治理任务不得与业务代码修改混跑。

## Failure Recovery

- Codex 卡住、失联、长时间无输出时，优先使用 control-plane 查询或中断：
  - `node .../codex-companion.mjs status`
  - `node .../codex-companion.mjs cancel ...`
  - `node .../codex-companion.mjs resume ...`，仅在非 `CX_DEGRADED` 状态下使用
  - `node .../codex-companion.mjs task-resume-candidate ...`
  - `codex status`
- `CX_DEGRADED` 状态下不得继续启动或恢复 Codex 执行线程；先报告失败阶段、最后有效进展和是否需要用户授权 CC break-glass。
- 失败报告至少包含：
  - 卡住或失败发生的阶段
  - 已执行到哪一步
  - 是否生成 patch
  - 是否改动文件
  - 可否安全重试
  - 需要的人工决策

### Failure Classification

单条命令报错不立即等同 `CX_DEGRADED`。Researcher / Executor 必须先按命令类别分流：

- `rejected: blocked by policy` 命中 `python -c` / `node -e` / `perl -e` /
  内联解释器脚本：read-only sandbox + approval never 配置下的预期行为，
  必须降级为 `Get-Content` / `Select-String` / `git grep` / `cmd /c type` /
  `cmd /c findstr` 重试一次，不得直接上报 `CX_DEGRADED`。
- `PropertySetterNotSupportedInConstrainedLanguageMode` 命中
  `[Console]::OutputEncoding = ...`：Windows ConstrainedLanguageMode 的预期行为，
  必须去掉该赋值后重试，不得直接上报 `CX_DEGRADED`。
- 基础只读命令（`Get-Content` / `Select-String` / `git grep` / `cmd /c type` /
  `cmd /c findstr` / `git status` / `git diff`）也被拒绝、或 `CreateProcessAsUserW
  failed: 5`、或线程无法启动：才进入 `CX_DEGRADED` 收敛动作。
- 同一阶段连续 3 次基础命令失败：进入 `CX_DEGRADED`，不得换壳硬闯。
