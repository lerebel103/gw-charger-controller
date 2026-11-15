# Dev workflow

## Verification
Run from the repo root or a worktree. The venv lives in the main checkout, so worktrees use its absolute path:

```
/Users/willc/Projects/gw-charger-controller/.venv/bin/python -m pytest tests/unit -q
/Users/willc/Projects/gw-charger-controller/.venv/bin/ruff check .
/Users/willc/Projects/gw-charger-controller/.venv/bin/ruff format --check .
```

## Main baseline (do new tests fail on main?)
Export main with `git archive <sha> | tar -x -C /tmp/<dir>`, then copy the new tests in and run pytest there. Do not stash or check out in the working tree.

## Git
- Follow the Git Policy in `AGENTS.md`: feature branch, no commit or push without permission, never touch main.
- Worktrees go in `.worktrees/`. No force push or amend after a push.
- Stage files by name. Never stage `.agents/`, `config.yaml`, root CSVs, or secrets (including any `mcp.json` with tokens).
- A hook (`.kiro/hooks/block-commit-on-main.json`) blocks `git commit` and `git push` while on main.

## PR comments
Address a comment only if it is relevant and not counter to the approach. See the `pr-feedback` skill.

## Data handoff
The user drops CSVs, PDFs and logs into `incoming/` at the repo root (gitignored). Look there first; chat attachments are not reliable.
