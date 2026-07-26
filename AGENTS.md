# claudecode Agent 规则

本文件是 Codex 在 `claudecode` 仓库内的常驻硬边界摘要。任务文档、工作区清单、脚本说明和细节流程由 `docs/` 索引发现，不在本文件重复维护。

## 不可违背规则

- 默认使用简体中文输出计划、进展、风险、验证结果和总结，除非用户明确要求其他语言。
- 计划文档、治理文档和任务总结正文必须为简体中文；英文计划视为不合格，交付前必须改为中文。
- 开始任务先运行 `git status --short`；发现非本轮修改时先报告并避免混入。
- 仓库根目录是治理、路由和工具骨架；业务修改必须落到已登记的明确工作区。
- `S/M/L` 只用于治理分级，不恢复旧 bridge 流程入口或权威流程。
- 通用需求澄清、编码纪律和自检由模型与本文件直接完成；仓库不建立通用流程 Skill、bridge、wrapper 或第二事实源。
- 不读取或修改凭据、token、auth、cookie、API key、proxy secret、`.env`、`auth.json`、`local.yaml`、`proxies.json`、`accounts.json`。
- 不把 Codex 出口、代理、账号池或路由维护混入普通仓库实现任务；本文件不保存 live proxy 细节、端口策略或维护步骤。
- 不覆盖、不回滚、不清理与当前任务无关的脏树改动；commit 前只允许 `git add` 本轮修改文件，禁止 `git add .`。
- 未获明确授权时，不主动 push、创建/更新 PR、merge、删除远端分支、丢弃未合并成果或执行难恢复 Git 操作。
- 用户明确授权 push、PR、merge、新建 worktree 或清理指定 branch/worktree 时，agent 必须自行执行必要命令并验证结果，不退回给用户手工输入。
- 用户当前轮明确要求修复已有开放 PR（审查意见、requested changes、CI failure 等闭环）时，任务默认包含验证、必要 commit 和普通 `git push` 到当前 PR 分支。
- PR 推送的前置校验、授权边界与停止条件以 `docs/当前规则/20-Git与高危操作.md` 的「PR 修复后的推送规则」为准；该授权不含 merge、tag、release、force push、rebase、历史重写或远端分支删除。
- 长任务按需要拆分阶段并逐段自检；不因任务规模自动派子智能体或设置 reviewer 门禁。
- `force push`、`reset --hard`、删除/丢弃未合并成果、覆盖远端历史，必须被用户明确点名；一旦动作和目标明确，不额外增加业务层确认。
- 不恢复 CC-CX guard、plan-gate、状态机、command、hook、memory、learning promotion、自动 PR shipping、task resume 或高权限 worktree skill。
- commit message 与 PR 标题/正文使用本仓约定的中文 conventional 格式；具体格式、例外和流程由 workflow 文档维护。

## Codex worker 退役状态

- Codex worker 已退役，不主动调用；历史材料与恢复步骤见 `docs/历史归档/cc-worker/README.md`，归档内容不是 active workflow 或恢复入口。

## 入口和收口

- Codex：先读本文件、`PROJECT.md` 和 `docs/index.md`，再按任务相关文档执行。
- 修复、排查、重构任务开工前，先查 `C:\Users\apple\kb\03 AI学习与实操\代码仓库维护\AI对话-踩坑速查.md`（历史会话踩坑索引，`needs-review`），命中同主题再读对应簇页。
- Claude Code：只读 `CLAUDE.md` 的入口说明；该文件不得覆盖本文件和仓库规则。
- 完成报告必须列出：修改文件、是否触碰登记业务工作区（清单见 `docs/当前规则/10-工作区登记.md`）、是否删除/清理/移动、是否 staging/commit/push、验证命令与结果、剩余风险。
- 仅在实际使用子智能体时记录其结果。
