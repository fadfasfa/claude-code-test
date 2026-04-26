# Module Admission

Use this document before adding or expanding repo-local workflow modules, hooks, tools, validation scripts, Playwright configuration, or skills outside the current accepted scope.

## Admission Card Template

- Name:
- Type:
- Solves:
- Does not solve:
- Trigger conditions:
- Reads:
- Writes:
- Installs dependencies:
- Runs browser:
- Affects git/worktree/global/kb:
- Disable:
- Delete:
- Minimal validation command:
- Why existing modules are insufficient:
- Status:

No admission card can authorize dangerous operations by itself.

## Current Cards

### continuous-execution-ledger

- Name: `continuous-execution-ledger`
- Type: ignored runtime ledger
- Solves: records current long-task plan, progress, next step, blockers, and resume notes
- Does not solve: permission grants, dangerous operation approval, task scheduling, learning promotion
- Trigger conditions: accepted multi-step plan, long task, context-risk task, resume after interruption
- Reads: `.tmp/active-task/current.md`, `docs/continuous-execution.md`
- Writes: `.tmp/active-task/current.md`
- Installs dependencies: no
- Runs browser: no
- Affects git/worktree/global/kb: ignored file only; no git/global/kb effect
- Disable: stop reading or updating the ledger
- Delete: remove `.tmp/active-task/current.md`
- Minimal validation command: `git check-ignore -v .tmp/active-task/current.md`
- Why existing modules are insufficient: root docs describe rules but do not hold runtime progress
- Status: accepted for repo-local runtime use

### stop-guard-lite

- Name: `stop-guard-lite`
- Type: candidate Stop / StopFailure hook
- Solves: reminds the agent when an accepted plan appears unfinished
- Does not solve: scheduling, auto-continue, permission bypass, business edits, dependency installs
- Trigger conditions: future explicit approval to add a Stop hook; ledger exists and says work is unfinished
- Reads: `.tmp/active-task/current.md`
- Writes: StopFailure may append local ignored diagnostics only if explicitly approved; Stop must write nothing
- Installs dependencies: no
- Runs browser: no
- Affects git/worktree/global/kb: no git/worktree/global/kb effect
- Disable: remove hook registration from repo-local settings after approval, or do not approve it
- Delete: delete the hook script and settings entry after approval
- Minimal validation command: dry-run the hook against a sample ignored ledger
- Why existing modules are insufficient: rules and skills rely on agent discipline; a lightweight Stop reminder catches accidental early stopping
- Status: proposal only; do not write hook without new user confirmation

### resume-active-task

- Name: `resume-active-task`
- Type: repo-local skill
- Solves: resumes a paused task from the ignored ledger and current git state
- Does not solve: bypassing safety boundaries, approving dangerous git, global/kb changes
- Trigger conditions: user says resume, continue previous task, or context was interrupted
- Reads: `.tmp/active-task/current.md`, `AGENTS.md`, `CLAUDE.md`, `docs/continuous-execution.md`, `git status`
- Writes: `.tmp/active-task/current.md` and current-task files only after normal task routing
- Installs dependencies: no
- Runs browser: no by default
- Affects git/worktree/global/kb: no global/kb effect; git only read unless separately approved
- Disable: do not invoke the skill
- Delete: remove `.claude/skills/resume-active-task/`
- Minimal validation command: `Get-Content .tmp/active-task/current.md -ErrorAction SilentlyContinue`
- Why existing modules are insufficient: generic task routing lacks resume-specific state reconciliation
- Status: accepted repo-local skill

### module-admission

- Name: `module-admission`
- Type: repo-local skill and doc template
- Solves: prevents ad hoc module, hook, tool, or Playwright additions
- Does not solve: implementation of the admitted module
- Trigger conditions: adding workflow module, hook, tool, validation script, Playwright config, or new skill
- Reads: `docs/module-admission.md`, relevant module docs
- Writes: admission proposal text; tracked docs only when the task explicitly asks to update admission records
- Installs dependencies: no
- Runs browser: no
- Affects git/worktree/global/kb: no global/kb effect
- Disable: skip only when the user explicitly scopes a trivial docs-only edit
- Delete: remove `.claude/skills/module-admission/`
- Minimal validation command: inspect the completed card against the required fields
- Why existing modules are insufficient: existing docs do not force a consistent pre-write card
- Status: accepted repo-local skill

### frontend-polish-lite

- Name: `frontend-polish-lite`
- Type: repo-local skill
- Solves: lightweight frontend UI polish and validation
- Does not solve: full design system, product redesign, dependency setup, visual QA for every task
- Trigger conditions: frontend task, UI interaction, page behavior, screenshot, responsive check, visual regression, accessibility smoke check
- Reads: changed frontend files, nearest README/design notes, `docs/frontend-validation.md`, `docs/playwright-policy.md`
- Writes: current-task frontend files only when the user asked for implementation
- Installs dependencies: no
- Runs browser: only when needed for headed, screenshot, or trace validation
- Affects git/worktree/global/kb: no global/kb effect; no worktree unless separately planned
- Disable: do not invoke the skill
- Delete: remove `.claude/skills/frontend-polish-lite/`
- Minimal validation command: `playwright --version`
- Why existing modules are insufficient: `frontend-patterns` covers coding patterns but not a focused UI polish acceptance loop
- Status: accepted repo-local skill

### review-diff

- Name: `review-diff`
- Type: repo-local skill
- Solves: consistent diff review and verification reporting
- Does not solve: GitHub PR creation, merge, push, or automatic approval
- Trigger conditions: user asks for review, before commit, after medium/large patch
- Reads: `git status`, `git diff`, changed files, relevant docs/tests
- Writes: review report only unless the user asks for fixes
- Installs dependencies: no
- Runs browser: no by default
- Affects git/worktree/global/kb: git read only; no global/kb effect
- Disable: do not invoke the skill
- Delete: remove `.claude/skills/review-diff/`
- Minimal validation command: `git diff --stat`
- Why existing modules are insufficient: `requesting-code-review` is request-oriented; this provides a local diff review checklist
- Status: accepted repo-local skill

### self-improvement-promotion

- Name: `self-improvement-promotion`
- Type: repo-local skill
- Solves: turns raw local error/learning inputs into user-reviewed repo learning candidates
- Does not solve: automatic global learning, automatic kb updates, raw log commits
- Trigger conditions: user asks to review/promote learnings or a task produces repeated repo-local errors
- Reads: `.learnings/ERRORS.md`, `.learnings/LEARNINGS.md`, relevant repo-local skills/hooks/tools
- Writes: `.learnings/LEARNINGS.md` only after user confirmation; never writes raw logs into git
- Installs dependencies: no
- Runs browser: no
- Affects git/worktree/global/kb: no global/kb effect
- Disable: do not invoke the skill
- Delete: remove `.claude/skills/self-improvement-promotion/`
- Minimal validation command: `git check-ignore -v .learnings/ERRORS.md`
- Why existing modules are insufficient: existing error logging captures raw data but does not enforce human-reviewed promotion
- Status: accepted repo-local skill

### playwright-config-or-script

- Name: `playwright-config-or-script`
- Type: future module candidate
- Solves: repeatable frontend validation command for a specific work area
- Does not solve: dependency installation, global validation, kb validation
- Trigger conditions: a frontend work area needs repeated Playwright validation beyond ad hoc CLI use
- Reads: target work-area frontend files, local app start docs, `docs/playwright-policy.md`
- Writes: only approved repo-local config or script paths named in a future card
- Installs dependencies: no by default; installation requires separate confirmation
- Runs browser: yes, only for approved validation tasks
- Affects git/worktree/global/kb: tracked repo-local file only; no global/kb effect
- Disable: stop using the script/config
- Delete: remove the approved config/script files
- Minimal validation command: to be defined by future target work area
- Why existing modules are insufficient: ad hoc CLI is enough until repeated validation needs a stable command
- Status: future card required before writing any config or script

### claudecode-ecc-residue-retirement

- Name: `claudecode-ecc-residue-retirement`
- Type: repo-local cleanup proposal
- Solves: inventories and classifies ECC residue inside `C:\Users\apple\claudecode`
- Does not solve: global ECC retirement, global file deletion, active CLI changes
- Trigger conditions: user starts a claudecode-local ECC cleanup task
- Reads: repo-local docs, settings, hooks, skills, tools
- Writes: cleanup report first; deletion/archive requires later confirmation
- Installs dependencies: no
- Runs browser: no
- Affects git/worktree/global/kb: repo-local only; no global/kb effect
- Disable: do not run cleanup
- Delete: remove only approved claudecode-local residue in a later task
- Minimal validation command: search repo-local text files for `ECC` / `Enhanced Context Craft`
- Why existing modules are insufficient: generic safety docs do not classify delete/archive/backup candidates
- Status: proposal only; no deletion in this task
