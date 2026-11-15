"""Tests for scripts/hooks/block_commit_on_main.sh (Kiro PreToolUse hook)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "block_commit_on_main.sh"


def _repo(path: Path, branch: str) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    return str(path)


@pytest.fixture
def repos(tmp_path):
    return _repo(tmp_path / "main_repo", "main"), _repo(tmp_path / "feat_repo", "feat")


def _run(command: str, cwd: str) -> int:
    payload = json.dumps({"tool_name": "execute_bash", "tool_input": {"command": command, "cwd": cwd}})
    return subprocess.run(["sh", str(SCRIPT)], input=payload, capture_output=True, text=True).returncode


def test_commit_on_main_cwd_blocked(repos):
    main, _ = repos
    assert _run("git commit -m x", main) == 2


def test_commit_on_feature_cwd_allowed(repos):
    _, feat = repos
    assert _run("git commit -m x", feat) == 0


def test_quoted_git_c_main_blocked_from_feature_cwd(repos):
    main, feat = repos
    assert _run(f'git -C "{main}" commit -m x', feat) == 2


def test_unrelated_git_c_first_does_not_hide_main(repos):
    main, feat = repos
    assert _run(f"git -C {feat} status && git -C {main} commit -m x", feat) == 2


def test_cd_then_commit_fails_closed(repos):
    main, feat = repos
    assert _run(f"cd {main} && git commit -m x", feat) == 2


def test_git_dir_fails_closed(repos):
    _, feat = repos
    assert _run("git --git-dir=/tmp/x/.git commit -m x", feat) == 2


def test_variable_target_fails_closed(repos):
    _, feat = repos
    assert _run("git -C $REPO commit -m x", feat) == 2


def test_missing_directory_fails_closed(repos):
    _, feat = repos
    assert _run("git -C /nonexistent/dir commit -m x", feat) == 2


def test_non_write_command_allowed(repos):
    main, _ = repos
    assert _run("git status", main) == 0
