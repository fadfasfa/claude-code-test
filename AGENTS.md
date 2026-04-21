# 仓库级稳定规则 — claudecode
> 本文件只定义 agent 执行、读写边界和审查边界。仓库结构索引看 `PROJECT.md`，能力层基线看 `agent_tooling_baseline.md`。

## 一、默认规则链

- `AGENTS.md`、`CLAUDE.md`、`PROJECT.md`、`work_area_registry.md`、`agent_tooling_baseline.md` 是本仓默认规则源。
- 上述限制只约束默认规则依据，不限制为完成当前任务而按需读取当前工作区文件。
- 其他路径不是默认规则依据。
- Desktop / OneDrive / “各个设定及工作流” 支持层仅在用户显式要求审计、比对或迁移时读取。
- 历史或残留路径如 `.ai_workflow/*`、`.claude/worktrees/*`、旧 `.agents/*`、`archive/**`、`.gitnexus/**` 只按 historical note / residual check 处理，不作为默认规则源或默认参考源。

## 二、工作区读写边界

- 本仓是多工作区仓；先在 `work_area_registry.md` 确认目标工作区，再开始写入。
- 默认允许跨仓读取；默认只允许在当前目标工作区目录树内写入。
- 当前任务涉及的当前工作区文件，仍可按需读取。
- 未明确目标工作区时，只允许只读探查并列出候选工作区，不得直接写文件。
- 仓库根目录不是默认业务写入区；没有明确仓库治理任务时，不在根目录写业务文件。
- 禁止跨工作区写入；不要为某个工作区任务改写别的工作区。
- 禁止在根目录创建抓取脚本目录、业务目录、输出目录、MCP 配置目录，禁止擅自安装 MCP 或写入 `.mcp.json`。
- 当前仓不需要项目级 `.codex/config.toml`；从仓库根启动时，默认只做只读探查或仓库治理，不做业务实现。

## 三、审查边界

- 审查主输入是 `diff`、任务说明、验证结果，以及必要时的 `PROJECT.md` 同步证据。
- 审查不以旧 runtime state、旧 contract 文档、历史快照或外围台账作为默认依据。
- 审查优先判断是否违反当前轻基线：默认规则源、工作区写边界、`plugin=0`、`MCP=0`、`hooks=0`。
