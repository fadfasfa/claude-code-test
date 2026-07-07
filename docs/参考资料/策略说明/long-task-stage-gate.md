# 长任务阶段验收

本文件是 Claude Code 与 Codex 在本仓执行长任务 / L 级任务时的阶段验收模板。它只提供可复制的轻量规范，不新增 command、skill、hook、wrapper、daemon、队列或状态机。

## 触发条件

满足任一条件时，任务必须先拆成阶段，并在每阶段完成后通过子智能体审查：

- 任务被判断为 `L`，或涉及 workflow、agent 规则、skill、hook、proxy、仓库结构、关键数据、策略、回测或跨模块修改。
- 任务需要连续多个可独立验收的阶段，且单次上下文容易过长。
- 任务会跨工作区、跨入口文档、跨执行 surface，或存在明显回滚/混改风险。
- 用户明确要求“长任务”“分阶段”“阶段验收”“子智能体审查”或等价流程。

`S` 和普通 `M` 任务不强制使用本流程；如任务执行中升级到以上条件，先停下重切阶段。

## 阶段包模板

每个阶段开始前，主线程先写清以下内容；无法写清时不要派 worker 或 reviewer。

```text
阶段名称：
阶段目标：
本阶段范围：
Owned files / modules：
禁止事项：
允许的验证命令：
完成证据：
进入下一阶段条件：
```

字段口径：

- `阶段目标` 只写本阶段可验收的结果，不写整件大任务的终局愿景。
- `本阶段范围` 和 `Owned files / modules` 必须足够窄，避免和用户已有脏改混在一起。
- `禁止事项` 至少包含不得触碰未授权工作区、凭据、Git 高危操作和本阶段外文件。
- `允许的验证命令` 必须是本阶段能实际运行的最小验证。
- `完成证据` 包含本地自检结果、diff 范围和必要输出摘要。
- `进入下一阶段条件` 至少要求主线程自检通过、`spec-compliance` PASS、`maintainability/code-quality` PASS。

## 阶段执行顺序

1. 主线程执行本阶段修改或调度本阶段 worker。
2. 主线程运行本阶段最小验证，并检查 diff 没有越界。
3. 派只读 `spec-compliance` reviewer。
4. `spec-compliance` 通过后，派只读 `maintainability/code-quality` reviewer。
5. 任一 reviewer 不通过时，主线程先修复，再重新执行本阶段验证和对应审查。
6. 两个 reviewer 都通过后，才进入下一阶段。

Review subagent 不参与权限升级；不得写文件、stage、commit、push、merge、rebase、改配置、读凭据或扩大任务范围。

## spec-compliance reviewer prompt

```text
你是本阶段的 spec-compliance reviewer。只读审查，不修改文件、index、分支、远端、配置或 PR。

默认使用简体中文输出审查结论、风险和建议；路径、命令和错误原文保持原文。

阶段目标：
<粘贴阶段目标>

本阶段范围：
<粘贴本阶段范围>

Owned files / modules：
<粘贴 owned files / modules>

禁止事项：
<粘贴禁止事项>

完成证据：
<粘贴主线程验证命令、结果摘要和 diff 摘要>

请检查：
1. 当前 diff 是否只完成本阶段目标。
2. 是否触碰本阶段外文件、未授权工作区、凭据或高危 Git / 配置边界。
3. 验证证据是否足以支持进入下一阶段。
4. 是否存在遗漏的验收条件。

输出格式：
- 结论：PASS 或 FAIL
- 阻断问题：
- 非阻断风险：
- 建议验证：
```

## maintainability / code-quality reviewer prompt

```text
你是本阶段的 maintainability / code-quality reviewer。只读审查，不修改文件、index、分支、远端、配置或 PR。

默认使用简体中文输出审查结论、风险和建议；路径、命令和错误原文保持原文。

阶段目标：
<粘贴阶段目标>

本阶段范围：
<粘贴本阶段范围>

Owned files / modules：
<粘贴 owned files / modules>

禁止事项：
<粘贴禁止事项>

完成证据：
<粘贴主线程验证命令、结果摘要和 diff 摘要>

请检查：
1. 是否引入重复事实源、路径漂移、过度抽象或隐式状态。
2. 是否恢复旧 CC-CX、cc-worker、hook、wrapper、daemon、队列、状态机或 Superpowers bridge。
3. 是否遗漏中文维护说明、边界说明或必要的最小验证。
4. 后续阶段是否会因为当前改动变得更难验收。

输出格式：
- 结论：PASS 或 FAIL
- 阻断问题：
- 非阻断风险：
- 建议验证：
```

## 收尾记录

长任务最终收尾仍遵守 `docs/当前规则/30-验证与审查.md`。报告必须列出每阶段的自检结果、两个 reviewer 结论、最终验证命令和剩余风险；没有实际运行的验证不得写成已通过。
