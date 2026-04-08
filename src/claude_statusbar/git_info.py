"""Git branch and worktree detection for the status bar.

All subprocess interaction is confined to this module. Every failure mode
collapses to None (or a dataclass with None fields) — never raises.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

CACHE_FILE = Path.home() / ".cache" / "claude-statusbar" / "default_branches.json"
SUBPROCESS_TIMEOUT_S = 0.2
_DETACHED_HEAD_SENTINEL = "HEAD"
_DEFAULT_BRANCH_FALLBACK = ("main", "master", "develop", "trunk", "staging")


@dataclass(frozen=True)
class GitInfo:
    """Result of a git probe.

    `branch` is None when the current branch matches the repo's default.
    `worktree` is None when the cwd is in the main worktree.
    A token is rendered only when its field is not None.
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

    # Resolve to absolute paths so the cache key is stable across cwds.
    git_dir = Path(git_dir_str)
    if not git_dir.is_absolute():
        git_dir = (cwd_path / git_dir).resolve()
    common_dir = Path(common_dir_str)
    if not common_dir.is_absolute():
        common_dir = (cwd_path / common_dir).resolve()

    # Worktree detection: secondary worktree iff git-dir != git-common-dir.
    if git_dir != common_dir:
        worktree: Optional[str] = git_dir.name
    else:
        worktree = None

    # Branch detection.
    if current_branch == _DETACHED_HEAD_SENTINEL:
        sha = _run_git(["rev-parse", "--short", "HEAD"], cwd=cwd)
        branch: Optional[str] = sha.strip() if sha else None
    else:
        default_branch = _load_default_branch(common_dir, cwd)
        if default_branch is not None:
            branch = None if current_branch == default_branch else current_branch
        else:
            # No remote or symbolic-ref failed — use the heuristic fallback set.
            branch = None if current_branch in _DEFAULT_BRANCH_FALLBACK else current_branch

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


def _load_default_branch(common_dir: Path, cwd: str) -> Optional[str]:
    """Look up the repo's default branch, caching the result on disk forever.

    Returns None if `git symbolic-ref refs/remotes/origin/HEAD` fails (no
    remote, unset HEAD, etc.). Callers should fall back to the heuristic
    set in that case. Failures are intentionally not cached so a later
    `git remote add origin` starts working without manual cache busting.
    """
    key = str(common_dir)
    cache = _read_cache()
    cached = cache.get(key)
    if cached:
        return cached

    output = _run_git(
        ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=cwd,
    )
    if not output:
        return None
    # Output is like "origin/main"; strip the remote prefix. We require a
    # "/" — an output without one is unexpected and not safe to cache as
    # a "default branch name."
    stripped = output.strip()
    if "/" not in stripped:
        return None
    name = stripped.split("/", 1)[1]
    if not name:
        return None
    cache[key] = name
    _write_cache(cache)
    return name


def _read_cache() -> Dict[str, str]:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_cache(data: Dict[str, str]) -> None:
    """Atomic write via tempfile + rename, mirroring cache.py::write_cache."""
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=CACHE_FILE.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.rename(tmp_path, CACHE_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError:
        # Cache write failed — silent. Lookup will re-run next render.
        pass
