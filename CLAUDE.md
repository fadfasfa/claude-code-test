# Claude Code Repo Entry

- 这是本仓的 Claude Code 入口文件，先遵守这里，再按需读取 `AGENTS.md`。
- `AGENTS.md` 仍是当前 workflow / contract 的主要来源；本阶段不在这里覆盖旧的分支或 worktree 语义。
- 历史 `.agents/*` workflow/contract 文档已退役；若看到旧路径引用，不把旧 `.agents/*` 当活跃入口。
- 实现前先看仓库现状、已有脚本、现有技能和相关文档。
- 当前阶段只做能力层基线：优先 CLI、先验证再完成、不自动 push / PR / merge / rebase，除非用户明确要求。
- 项目级 Claude 技能放在 `.claude/skills/`，按需显式使用。

## Work-Area Guardrails

- 先确认目标工作区；没有明确要求时，不在仓库根目录写业务文件。
- 默认只在当前目标工作区写入；不要为某个工作区任务改写别的工作区。
- 从仓库根启动时，只做只读探查、仓库治理或文档治理；从工作区目录启动时，才做该工作区实现。
- 浏览器自动化默认走 Playwright CLI，不安装 Playwright MCP。
- 未明确允许时，不新增 `.mcp.json`、`playwright-mcp/`、`mcp/` 或其他 MCP 目录。
