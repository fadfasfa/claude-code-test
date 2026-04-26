# Frontend Validation

Frontend validation in `claudecode` is lightweight and task-scoped. It is not a full design system and not a default step for every task.

## When To Use

Use this workflow for:

- frontend UI implementation
- page behavior changes
- interactive state changes
- responsive or narrow-screen layout checks
- visual regression risk
- obvious accessibility risk
- screenshot/headed/trace validation needs

Do not use it for backend-only tasks, docs-only edits, data-only changes, or trivial copy changes unless the user asks.

## Checks

Inspect the changed UI for:

- visual hierarchy
- spacing
- font size and weight
- color contrast and palette drift
- alignment
- hover / focus / active / disabled states
- loading / empty / error states
- mobile and narrow-screen layout
- obvious accessibility issues

## Playwright Use

Use Playwright only when it helps validate the current frontend task.

Allowed examples:

```powershell
playwright --version
playwright screenshot http://127.0.0.1:3000 .tmp/frontend-screenshot.png
```

Do not install Playwright, add config, add scripts, or write hooks without a module admission card and user confirmation.

## Completion Report

Frontend completion reports must include:

- what changed
- how it was validated
- browser/screenshot/trace evidence if used
- what still needs human visual confirmation

Use the `frontend-polish-lite` skill for the checklist.
