---
name: python-patterns
description: Project-level Python guidance focused on readability, small interfaces, and safe incremental edits.
---

# python-patterns

中文简介：本 skill 用于本仓 Python 代码修改。它约束可读性、窄接口、中文文件头和关键 docstring；不负责新增 hook、MCP、worktree 自动化或强制 branch 流程。

## 什么时候使用

处理本仓 Python code 时使用。

## 文档和注释

- 新 Python source files 必须以简短中文 module-level description 开头，说明目的和 runtime boundary。
- 关键函数、类和 script entrypoints 在角色、输入、副作用或安全边界不明显时，应添加 docstring。
- Hooks、tools 和 workflow-control scripts 必须说明安全边界，以及它们不得修改什么。
- 不堆砌重复明显代码行为的注释。

## 规则

- 优先清晰函数、显式命名和窄模块。
- 新增抽象前先复用已有 helpers。
- type hints 和 docstrings 与本地风格保持一致。
- 相关时用已有 lint、test 或 execution commands 验证。
- 不新增 hooks、MCP dependencies、worktree automation 或 forced branch flow。
