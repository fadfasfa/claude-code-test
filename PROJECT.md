# claudecode Project Map

`claudecode` 是个人总编程仓、多子项目母仓和本机 agent 执行仓。仓库根目录承载规则、路由和工具骨架，不承载默认业务实现。

Claude Code 与 Codex 均可独立工作；仓库不再维护固定的 CC-CX 强分工主流程。

## 入口职责

- `AGENTS.md`：Codex 常驻硬边界。
- `CLAUDE.md`：Claude Code 入口说明。
- `README.md`：人类快速入口。
- `docs/index.md`：唯一文档发现索引；任务相关 workflow、工作区清单、Git/PR 规则和脚本边界都从这里继续发现。

业务写入前必须先选定已登记工作区；具体工作区事实源和写入边界由 `docs/index.md` 指向的 workflow 文档维护。普通任务只产出目标 diff 和对话摘要。

## Non Goals

- 不用普通仓库任务修改全局工具配置。
- 不把子项目业务规则写入仓库根规则。
- 不恢复 CC-CX 强编排、command、hook、memory、learning、自动 PR shipping、task resume 或高权限 worktree 能力。
