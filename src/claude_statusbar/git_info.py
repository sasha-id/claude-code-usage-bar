"""Git branch and worktree detection for the status bar.

All subprocess interaction is confined to this module. Every failure mode
collapses to None (or a dataclass with None fields) — never raises.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

SUBPROCESS_TIMEOUT_S = 0.2
_DETACHED_HEAD_SENTINEL = "HEAD"


@dataclass(frozen=True)
class GitInfo:
    """Result of a git probe.

    `branch` is the current branch name (or short SHA when detached).
    `worktree` is None when the cwd is in the primary worktree; otherwise
    it is the secondary worktree's directory name.
    """
    branch: Optional[str] = None
    worktree: Optional[str] = None


def get_git_info(cwd: Optional[str]) -> Optional[GitInfo]:
    """Return git info for `cwd`, or None if cwd is missing/not a repo/git unavailable."""
    if not cwd:
        return None
    cwd_path = Path(cwd)
    if not cwd_path.is_dir():
        return None

    output = _run_git(
        ["rev-parse", "--git-dir", "--git-common-dir", "--abbrev-ref", "HEAD"],
        cwd=cwd,
    )
    if output is None:
        return None
    lines = output.splitlines()
    if len(lines) != 3:
        return None
    git_dir_str, common_dir_str, current_branch = lines

    # Resolve relative paths against cwd so comparisons are meaningful.
    git_dir = Path(git_dir_str)
    if not git_dir.is_absolute():
        git_dir = (cwd_path / git_dir).resolve()
    common_dir = Path(common_dir_str)
    if not common_dir.is_absolute():
        common_dir = (cwd_path / common_dir).resolve()

    # Worktree detection: secondary worktree iff git-dir != git-common-dir.
    # In the primary worktree, both point at the same `.git` directory.
    # In a linked worktree, git-dir is `.git/worktrees/<name>` while
    # common-dir is still the repo's top-level `.git`.
    if git_dir != common_dir:
        worktree: Optional[str] = git_dir.name
    else:
        worktree = None

    # Branch: always shown. Detached HEAD → short SHA instead of the
    # literal "HEAD" sentinel that --abbrev-ref returns.
    if current_branch == _DETACHED_HEAD_SENTINEL:
        sha = _run_git(["rev-parse", "--short", "HEAD"], cwd=cwd)
        branch: Optional[str] = sha.strip() if sha else None
    else:
        branch = current_branch or None

    return GitInfo(branch=branch, worktree=worktree)


def _run_git(args: List[str], cwd: str) -> Optional[str]:
    """Run a git subprocess; return stdout text or None on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout
