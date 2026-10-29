# claudecode Agent 规则

本文件是 Codex 在 `claudecode` 仓库内的常驻硬边界摘要。任务文档、工作区清单、脚本说明和细节流程由 `docs/` 索引发现，不在本文件重复维护。

## 不可违背规则

- 开始任务先运行 `git status --short`；发现非本轮修改时先报告并避免混入。
- 仓库根目录是治理、路由和工具骨架；业务修改必须落到已登记的明确工作区。
- `S/M/L` 只用于治理分级，不恢复旧 bridge 流程入口或权威流程。
- 通用需求澄清、编码纪律和自检由模型与本文件直接完成；仓库不建立通用流程 Skill、bridge、wrapper 或第二事实源。
- 不读取或修改凭据、token、auth、cookie、API key、proxy secret、`.env`、`auth.json`、`local.yaml`、`proxies.json`、`accounts.json`。
- 不把 Codex 出口、代理、账号池或路由维护混入普通仓库实现任务；本文件不保存 live proxy 细节、端口策略或维护步骤。
- 不覆盖、不回滚、不清理与当前任务无关的脏树改动。
- Codex 的普通分支、工作树、提交与普通推送授权语义以用户级 `AGENTS.md` 为准；本仓只在 `docs/当前规则/20-Git与高危操作.md` 维护保护资产、工作树路径、PR 修复、清理边界和提交格式。Claude Code 继续使用自己的用户级授权规则。
- 用户当前轮明确要求修复已有开放 PR（审查意见、requested changes、CI failure 等闭环）时，任务默认包含验证、必要 commit 和普通 `git push` 到当前 PR 分支。
- PR 推送的前置校验、授权边界与停止条件以 `docs/当前规则/20-Git与高危操作.md` 的「PR 修复后的推送规则」为准；该授权不含 merge、tag、release、force push、rebase、历史重写或远端分支删除。
- Codex 按用户级 `AGENTS.md` 按需委派并复核结果；不因任务规模设置固定 reviewer 门禁。Claude Code 遵循自己的用户级规则及本仓跨工具调用边界。
- 不恢复 CC-CX guard、plan-gate、状态机、command、hook、memory、learning promotion、自动 PR shipping、task resume 或高权限 worktree skill。Codex worker 已退役，不主动调用；`docs/历史归档/cc-worker/README.md` 仅供历史查阅，不是执行或恢复入口。
- commit message 与 PR 标题/正文使用本仓约定的中文 conventional 格式；具体格式、例外和流程由 workflow 文档维护。

## 入口和收口

- Codex 按本文件和用户任务执行；项目定位或根目录职责不清时读 `PROJECT.md`，需要专项规则时从 `docs/index.md` 选读。已加载且未变化的规则不重复通读。
- 涉及旧架构、重复故障或现场证据不足时，按主题查 `C:\Users\apple\kb\03 AI学习与实操\代码仓库维护\AI对话-踩坑速查.md`，命中后再读对应簇页。`needs-review` 历史摘要只作线索，以当前文件和原始证据为准。
- Claude Code 从 `CLAUDE.md` 进入；两端共用仓库规则，各自使用原生工具和权限入口。
- 修改任务按 `docs/当前规则/30-验证与审查.md` 报告实际变更、验证和关键未完成项；纯问答不附无关操作回执。
