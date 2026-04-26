<!-- claudecode-repo-local -->
---
name: frontend-polish-lite
description: Lightweight claudecode-only frontend UI polish and validation for visual, responsive, interaction, and accessibility smoke checks.
---

# frontend-polish-lite

Use this skill for frontend tasks that need UI polish or validation.

## Scope

Check:

- visual hierarchy
- spacing
- typography
- color and contrast
- alignment
- hover / focus / active / disabled states
- loading / empty / error states
- mobile and narrow-screen layout
- obvious accessibility issues

## Playwright

Use Playwright only when it helps the current task.

Allowed:

- `playwright --version`
- screenshot / headed / trace validation against a local or file URL
- writing screenshots or traces under ignored `.tmp/`

Not allowed without a module admission card and user confirmation:

- installing Playwright
- adding `playwright.config.*`
- adding npm scripts or validation scripts
- adding hooks
- adding MCP
- making Playwright a default step for all tasks

## Rules

- This skill is claudecode-only.
- Do not apply it to `kb`.
- Do not write global hooks or global skills.
- Do not turn a lightweight polish pass into a full design system.
- Do not redesign the product unless the user asks.

## Completion Report

Include:

- what changed
- how it was validated
- whether Playwright was used
- whether human visual confirmation is still needed
