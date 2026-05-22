# Claude Code Entry

本文件是 Claude Code 在本仓的独立入口。Claude Code 可以直接完成探查、修改、验证和提交；Codex 也可以独立工作，二者互不依赖。

## Default Flow

- 默认使用简体中文。
- 开始任务先运行 `git status --short`，发现非本轮修改时先报告并避免混入。
- 只读探查可以直接执行；用户当前轮明确要求实现、修复、调整或修改时，普通仓库文件编辑可以直接执行，仍需按授权范围小步修改并验证。
- 涉及 workflow/config/skill/hook 修改、git 写操作、worktree 操作、删除/移动/覆盖等破坏性命令、越界路径、敏感文件、依赖或环境变更、外部账户或真实网络副作用时，必须先输出计划并等待用户确认。
- 计划必须包含：`git status`、预计修改文件、修改内容、不修改范围、验证命令、Git 处理方式；确认后按计划小步执行，范围变化时重新确认。
- 先读 `PROJECT.md`、`docs/index.md` 和任务相关 workflow 文档，再选择目标工作区。
- 修改保持小步、明确范围，不顺手重构业务代码。
- 修改后运行最小有效验证；无法验证时说明具体原因。
- 验证通过后，若本轮允许提交，只暂存本轮修改文件并 commit；禁止 `git add .`。

## Code Documentation

- 新增或修改的代码注释一律使用简体中文；标识符、API 名、命令、错误原文保留英文。
- 修改一个文件时，对路过的英文行内/块注释做顺手翻译；不主动扫全仓回填，不为翻译额外起 PR。
- agent 新建源代码或文档文件时，文件首部必须带中文头部说明：
  - Python/JS/TS：3 行以内 module docstring 或顶部块注释，说明该文件的职责、调用方、关键依赖。
  - Markdown：首行中文 `#` 标题 + 1 行中文简介。
  - PowerShell/Bash：脚本首行 shebang 或 `#Requires` 之后，紧跟 1–3 行中文注释。
- 实质修改老文件（非纯重命名、非纯格式化）且其缺少中文头部时，按上一条格式补一段；仅做最小补全，不顺手重写已有英文头部。
- 头部说明描述"做什么"和"谁会调它"，不写任务编号、PR 号或本轮改动说明。
- 不替换或翻译现有的中文头部；不为图标、二进制、生成产物、第三方 vendored 代码增加头部。

## Independent Work

- Claude Code 不需要调用 Codex 才能读取、修改或验证仓库文件。
- Codex 不需要通过 Claude Code 才能执行任务。
- 即使 OpenAI Codex plugin 可用，Claude Code 也不得在无用户当前轮显性命令时调用、委派、审查或触发 Codex / CX。
- 不再强制 Claude Code 计划、Codex 执行、Claude Code 验收的固定分工。

## Safety

- 不读取或修改 `.env`、`auth.json`、`local.yaml`、`proxies.json`、token、cookie、API key 或 proxy secret。
- `git push`、PR、merge、tag、release 等发布或合并动作未获用户明确授权时禁止主动执行；用户明确要求后由 agent 自行执行并验证结果，不得要求用户手动输入命令。
- discard / 清理类操作（`git reset --hard`、`git clean -fdx`、`git restore`、覆盖式 checkout、大范围删除和不可逆清理）未获用户明确批准时禁止主动执行；批准后由 agent 按确认范围执行，不得要求用户手动输入命令。
- 删除、覆盖、移动前先确认目标路径；需要备份时先确认备份成功。

## Retired Workflow Note

旧 CC-CX 强编排、Guard 状态机和 break-glass 流程已经退役，不作为 Claude Code 日常入口。
