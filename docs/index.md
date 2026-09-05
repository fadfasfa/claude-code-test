# 文档索引

`docs/` 的唯一发现索引。按当前任务选择相关条目，不要求预读全表；不要把参考资料、历史归档、方案草稿或方法论规格整体注入上下文。

## 按任务选读

| 任务类型 | 读取文档 |
| :--- | :--- |
| 默认执行流、中文输出、长任务阶段拆分 | `docs/当前规则/00-入口总览.md` |
| 工作区选择、写入边界、保护目录 | `docs/当前规则/10-工作区登记.md` |
| Git 高危操作、删除、清理、worktree、commit、push、PR | `docs/当前规则/20-Git与高危操作.md` |
| 验证、审查、收尾报告 | `docs/当前规则/30-验证与审查.md` |
| skill inventory、agent surface、重复 skill 维护 | `docs/当前规则/40-Agent与Skill.md` |
| 新增或扩展长期 workflow module、skill、hook、tool、验证脚本或自动化 | `docs/参考资料/策略说明/module-admission.md` |
| 仓库目录职责、路径生命周期 | `docs/当前规则/50-目录职责.md` |
| Codex surface、旧 CC-CX 退役、出口维护、Ultraplan | `docs/当前规则/90-退役边界.md` |
| Hextech、Overlay、Sidecar、打包部署或真机诊断 | `run/docs/README.md`，再按索引读取 `run/docs/overlay-runtime.md` |

## 参考资料

- `docs/参考资料/策略说明/task-routing.md`：`S/M/L` 概念说明；只定义薄边界。
- `docs/参考资料/`：按需读取的参考资料。
- `docs/历史归档/`：历史报告和退役资料，包含已退役的 cc-worker 材料；默认不作为当前规则来源。
- `docs/方案草稿/`：设计草稿，不作为默认事实源。
- `docs/方法论规格/`：方法论规格和计划，不作为默认事实源。

## 产物边界

普通任务不默认生成 `docs/方案草稿/*.md`、Markdown report、probe 或 archive 证据文件。
旧 `.state/workflow/**` 只作为 CC-CX 工作流遗留运行态；当前工作流不依赖旧任务结果文件。
