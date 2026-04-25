# Self-Improvement Review Input

Run from repo root:

```powershell
python .claude/tools/learning-loop/check_learning_loop.py
```

Run from this folder:

```powershell
python check_learning_loop.py
```

- This check is read-only.
- `.learnings/LEARNINGS.md` is the tracked evolution log for this repository.
- `.learnings/ERRORS.md` is ignored local/raw error input, not the main entry.
- It may review repeated errors and propose CC skill changes.
- It may execute approved repo-local CC skill changes only inside a user-authorized task.
- It may propose rule, skill, hook, or global sync changes, but global sync requires human review before synchronization.
- Every self-improvement evolution must add an entry to `.learnings/LEARNINGS.md`.
- It must not auto-loop, auto-edit rules, create global skills, write global config, modify `kb`, or interfere with CX App / Codex memory.
- One-off experience must not be promoted to long-term rules without explicit approval.
- Repo-local hooks may append failure records for later human review.
