<!-- claudecode-repo-local -->
---
name: self-improvement-promotion
description: Promote claudecode repo-local learning candidates from raw logs into tracked learning only after user review.
---

# self-improvement-promotion

Use this skill when the user asks to review or promote repo-local learnings.

## Sources

- Raw error cache: `.learnings/ERRORS.md`
- Tracked repo learning: `.learnings/LEARNINGS.md`
- Relevant repo-local skills/hooks/tools/docs

## Rules

- Raw logs are ignored inputs, not git content.
- Do not auto-promote learnings.
- Do not write global learning from this repo workflow.
- Do not modify `kb`.
- Do not mix runtime ledger entries with learning.
- Promote only stable, reusable, repo-local lessons.

## Promotion Checklist

For each candidate, report:

- source
- repeated or one-off
- repo-local value
- proposed target section
- exact wording
- risk of overgeneralization
- whether user confirmation is required

## Write Boundary

Only update `.learnings/LEARNINGS.md` after the user confirms the candidate list.
