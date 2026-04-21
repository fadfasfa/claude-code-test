# 项目文档 — claudecode workspace
> 本文件只保留当前仓库结构、工作区地图和索引入口；详细规则看 `AGENTS.md`，Claude 入口提醒看 `CLAUDE.md`，能力层基线看 `agent_tooling_baseline.md`。

## 一、项目总览

本仓库是一个多项目工作区，当前承载若干独立工作区项目，以及少量仓库级治理文档与能力层配置。

默认入口索引如下：

- `PROJECT.md`：项目/工作区稳定说明
- `AGENTS.md`：agent 读写边界与审查规则
- `CLAUDE.md`：Claude Code 根入口与读取顺序
- `work_area_registry.md`：工作区注册表与默认写边界
- `agent_tooling_baseline.md`：能力层基线与禁用项

这五个文件构成默认规则链。其他路径不是默认规则依据，但当前任务涉及的当前工作区文件仍可按需读取。

## 二、当前工作区结构

| 文件 / 目录 | 角色 |
| :--- | :--- |
| `PROJECT.md` | 当前仓库结构与索引 |
| `AGENTS.md` | agent 执行、读写、审查规则 |
| `CLAUDE.md` | Claude Code 根入口提醒 |
| `work_area_registry.md` | 工作区注册表 |
| `agent_tooling_baseline.md` | 能力层基线 |
| `.claude/` | 项目级 settings、skills、预留接口 |
| `run/` | Hextech 主运行工作区 |
| `qm-run-demo/` | Demo/runtime 变体工作区 |
| `heybox/` | 抓取脚本工作区 |
| `QuantProject/` | 量化策略与数据工作区 |
| `sm2-randomizer/` | Space Marine 2 randomizer 工作区 |
| `subtitle_extractor/` | 字幕提取工作区 |

## 三、历史说明

- `.ai_workflow/`、`.claude/worktrees/**`、旧 `.agents/*`、`archive/**` 等若仍存在，只代表历史留痕或残留镜像，不代表当前运行态。
- Desktop / OneDrive / “各个设定及工作流” 支持层不是仓内默认规则链，只有做显式审计、比对或迁移时才读取。
- 当前仓的默认写边界与工作区地图以 `work_area_registry.md` 为准。
