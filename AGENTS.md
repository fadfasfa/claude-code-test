# claudecode Agent 规则

本文件是 Codex 在 `claudecode` 仓库内的常驻硬边界摘要。任务文档、工作区清单、脚本说明和细节流程由 `docs/` 索引发现，不在本文件重复维护。

## 不可违背规则

- 默认使用简体中文输出总结、风险、验证结果和变更说明。
- 开始任务先运行 `git status --short`；发现非本轮修改时先报告并避免混入。
- 仓库根目录是治理、路由和工具骨架；业务修改必须落到已登记的明确工作区。
- `S/M/L` 只用于治理分级，不恢复旧 bridge 流程入口或权威流程。
- 官方 Superpowers plugin 是唯一 Superpowers 来源；仓库不提供旧 bridge、旧 wrapper 或本地 skill 作为第二来源。
- 不读取或修改凭据、token、auth、cookie、API key、proxy secret、`.env`、`auth.json`、`local.yaml`、`proxies.json`、`accounts.json`。
- 不把 Codex 出口、代理、账号池或路由维护混入普通仓库实现任务；本文件不保存 live proxy 细节、端口策略或维护步骤。
- 不覆盖、不回滚、不清理与当前任务无关的脏树改动；commit 前只允许 `git add` 本轮修改文件，禁止 `git add .`。
- 未获明确授权时，不主动 push、创建/更新 PR、merge、删除远端分支、丢弃未合并成果或执行难恢复 Git 操作。
- 用户明确授权 push、PR、merge、新建 worktree 或清理指定 branch/worktree 时，agent 必须自行执行必要命令并验证结果，不退回给用户手工输入。
- `force push`、`reset --hard`、删除/丢弃未合并成果、覆盖远端历史，必须被用户明确点名；一旦动作和目标明确，不额外增加业务层确认。
- 不恢复 CC-CX guard、plan-gate、状态机、command、hook、memory、learning promotion、自动 PR shipping、task resume 或高权限 worktree skill。
- commit message 与 PR 标题/正文使用本仓约定的中文 conventional 格式；具体格式、例外和流程由 workflow 文档维护。

## Codex 主动 worker 评估

- Codex 处理本仓非琐碎 S1/M1/M2 任务时，默认先主动评估是否使用 `scripts/ai/cc-worker.ps1` 辅助执行、方案探索或反向 review。
- 该评估是高权重提示词规则，不是 hook、daemon、任务队列、命令拦截或旧 CC-CX bridge；worker 仍只能由 Codex 显式短调用。
- 适合委派时，Codex 必须按 `docs/workflows/codex-cc-lightweight-worker.md` 写清任务包、允许写入范围、保护范围、验证命令和输出要求，并在调用后审查真实 diff。
- 以下情况默认不自动委派：L 级或仓库级核心策略、敏感凭据/认证/路由、强耦合根因排查、Git 高危/发布/PR/merge/rebase/tag/amend/reset/clean、必须整体理解的任务、以及 worker 开关关闭时。
- worker 不可用或被暂停时，Codex 继续独立执行当前任务，并在总结里说明未使用 worker 的原因。

## 入口和收口

- Codex：先读本文件、`PROJECT.md` 和 `docs/index.md`，再按任务相关文档执行。
- Claude Code：只读 `CLAUDE.md` 的入口说明；该文件不得覆盖本文件和仓库规则。
- 完成报告必须列出：修改文件、是否触碰 `run/**` 或 `QuantProject/**`、是否执行删除/清理/移动、是否 staging/commit/push、验证命令与结果、剩余风险。
