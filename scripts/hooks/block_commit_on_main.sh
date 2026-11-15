#!/bin/sh
# Kiro PreToolUse hook for execute_bash: block `git commit` / `git push` while on main.
# Reads the tool-call JSON on stdin. Exit 2 (with a message on stderr) blocks the call; exit 0 allows it.
# Uses only POSIX tools (no jq or python needed).
# Fails closed: if the target repository cannot be determined from a literal `git -C <path>` or the JSON "cwd", the call is blocked.
input=$(cat)
printf '%s' "$input" | grep -Eq 'git[[:space:]]+([^|&;]*[[:space:]])?(commit|push)([[:space:]"\\]|$)' || exit 0

block() {
  echo "Blocked: $1" >&2
  exit 2
}

# The target repo is unknowable if the command changes directory or redirects git via env/flags.
if printf '%s' "$input" | grep -Eq '(^|[^[:alnum:]_.-])(cd|pushd)[[:space:]]|--git-dir|--work-tree|GIT_DIR=|GIT_WORK_TREE='; then
  block "cannot tell which repository this git commit/push targets (cd/pushd, --git-dir, --work-tree or GIT_DIR/GIT_WORK_TREE). Use a literal 'git -C <path>' or the tool's cwd parameter."
fi

cwd=$(printf '%s' "$input" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
[ -n "$cwd" ] || cwd=.

# Candidates: the cwd plus every `git -C <dir>` target (bare or quoted).
targets=$(printf '%s' "$input" | grep -Eo "git[[:space:]]+-C[[:space:]]+(\\\\?[\"'])?[^[:space:]\"'\\\\]+" |
  sed -e 's/^git[[:space:]]*-C[[:space:]]*//' -e "s/^\\\\\\{0,1\\}[\"']//")
candidates="$cwd
$targets"

while IFS= read -r d; do
  [ -n "$d" ] || continue
  case "$d" in
    *'$'* | *'`'*) block "git -C target '$d' contains a shell expansion, so the repository cannot be verified. Use a literal path." ;;
  esac
  case "$d" in
    /*) ;;
    *) d="$cwd/$d" ;;
  esac
  [ -d "$d" ] || block "directory '$d' does not exist, so the repository cannot be verified."
  if [ "$(git -C "$d" branch --show-current 2>/dev/null)" = "main" ]; then
    block "git commit/push on main is not allowed. Use a feature branch (ideally a worktree under .worktrees/) and open a PR."
  fi
done <<EOF
$candidates
EOF
exit 0
