# Playwright Policy

Playwright is a claudecode-only, coding-only frontend validation capability.

It does not enter:

- global core
- `kb`
- global hooks
- default flow for every task

## Current Discovery

Phase 0 discovery found a Playwright CLI on PATH:

```text
C:\Users\apple\AppData\Local\Programs\Python\Python311\Scripts\playwright.exe
Version 1.58.0
```

No root-level `playwright.config.*` was found. Existing `package.json` / `package-lock.json` files are in work-area or legacy worktree paths, notably `sm2-randomizer\pipeline\collect\wiki`.

This repository must not install Playwright or add Playwright configuration/scripts without a module admission card.

## Trigger Conditions

Use Playwright only for:

- frontend tasks
- UI interaction checks
- page behavior checks
- screenshot validation
- responsive checks
- trace/debug validation
- visual regression investigation

Do not use Playwright for backend, docs, data, repo-governance-only, or `kb` tasks unless the user explicitly asks.

## Allowed Scope

- Read current task frontend files.
- Run browser validation only when a local or file URL is available and the task needs it.
- Write screenshots/traces only under ignored runtime paths such as `.tmp/`.

## Forbidden Scope

- No global Playwright install.
- No repo dependency install without confirmation.
- No global hooks.
- No `kb` validation policy.
- No Playwright MCP.
- No automatic all-task validation.

## Future Config Or Script

Any future `playwright.config.*`, npm script, PowerShell helper, or hook must first pass `docs/module-admission.md`.
