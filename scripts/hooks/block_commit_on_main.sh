#!/bin/sh
# Kiro PreToolUse hook for execute_bash: block `git commit` / `git push` while on main.
# Reads the tool-call JSON on stdin. Exit 2 (with a message on stderr) blocks the call; exit 0 allows it.
# Uses only POSIX tools (no jq or python needed).
input=$(cat)
printf '%s' "$input" | grep -Eq 'git[[:space:]]+([^|&;]*[[:space:]])?(commit|push)([[:space:]"\\]|$)' || exit 0
# Directory to check: `git -C <dir>` if present, else the JSON "cwd" field, else the current directory.
dir=$(printf '%s' "$input" | sed -n 's/.*git[[:space:]]\{1,\}-C[[:space:]]\{1,\}\([^[:space:]"\\]\{1,\}\).*/\1/p' | head -n 1)
[ -n "$dir" ] || dir=$(printf '%s' "$input" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
[ -n "$dir" ] && [ -d "$dir" ] || dir=.
branch=$(git -C "$dir" branch --show-current 2>/dev/null)
if [ "$branch" = "main" ]; then
  echo "Blocked: git commit/push on main is not allowed. Use a feature branch (ideally a worktree under .worktrees/) and open a PR." >&2
  exit 2
fi
exit 0
