<!-- adopted-into-repo-baseline -->
---
name: python-patterns
description: Project-level Python guidance focused on readability, small interfaces, and safe incremental edits.
---

# python-patterns

Use this skill for Python code in this repo.

## Documentation and comments

- New Python source files must start with a concise 中文（Chinese） module-level description of purpose and runtime boundary.
- Add docstrings to key functions, classes, and script entrypoints when their role, inputs, side effects, or safety boundary is not obvious from names.
- Hooks, tools, and workflow-control scripts must document their safety boundary and what they must not modify.
- Do not pile up comments that merely repeat obvious code behavior.

## Rules

- Prefer clear functions, explicit names, and narrow modules.
- Reuse existing helpers before adding new abstractions.
- Keep type hints and docstrings aligned with local conventions.
- Validate with existing lint, test, or execution commands when relevant.
- No hooks, MCP dependencies, worktree automation, or forced branch flow.
