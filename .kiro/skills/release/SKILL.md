---
name: release
description: Cut a new version of the GW charger controller (version-bump PR, tag, GitHub release). Use when the user asks to release or bump the version.
---

# Release

Mirror v0.1.13 (PR #26, branch `release/v0.1.13`, commit `chore(release): v0.1.13`).

1. Ask the user for the version `X.Y.Z` and confirm main contains everything to ship.
2. Create a branch `release/vX.Y.Z` (in a worktree under `.worktrees/`). Never commit on main.
3. Update the version in `app/version.py` (`__version__`), `pyproject.toml` and `uv.lock` (PR #26 touched exactly these three files).
4. Run the verification commands from the dev-workflow steering. Commit `chore(release): vX.Y.Z` only with the user's permission, push the branch and open a PR.
5. After the user merges the PR, tag main with `vX.Y.Z` and push the tag (ask first). Pushing a `v*` tag triggers `.github/workflows/release.yml`, which builds the changelog from `git log` since the previous tag and runs `gh release create` with generated notes and the Docker pull command. Nothing else is needed for the GitHub release.
6. Check the Release workflow run succeeded and the release page lists the changes.
