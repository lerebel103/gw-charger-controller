---
name: pr-feedback
description: Triage and address review and Copilot comments on a pull request with the gh CLI. Use when the user says there are comments on the PR.
---

# PR feedback

1. Fetch the comments: `gh pr view <n> --comments`, `gh api repos/{owner}/{repo}/pulls/<n>/comments` (inline) and `gh api repos/{owner}/{repo}/pulls/<n>/reviews`.
2. Triage each as relevant or counter to the approach (check the design-decisions steering). Record the result in `.agents/tasks/` (local, not committed).
3. Fix the relevant ones on the PR branch. No history rewrite, no commit or push without permission.
4. Run the verification commands from the dev-workflow steering.
5. After the user approves and pushes, reply to each comment (say what changed, or why it was declined) and resolve the threads.
