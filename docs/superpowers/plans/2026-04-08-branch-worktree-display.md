# Branch and Worktree Display + Icon Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
1. Add conditional git branch and worktree tokens to the status bar — branch shown only when not the repo's default, worktree shown only when in a secondary worktree.
2. Remove the `⏰` alarm-clock prefix on the 5h/7d reset times and the `get_countdown_emoji` system (`🎉` / `✨` / `⚡`) entirely.

**Architecture:** A new self-contained module `git_info.py` owns all subprocess interaction. `core.py` calls it once per render and threads the result into `progress.format_status_line` via two new optional kwargs. A `--no-git` opt-out lives in `cli.py`. Default-branch lookups are cached forever in `~/.cache/claude-statusbar/default_branches.json`, keyed by absolute `git-common-dir`. Icon cleanup is a straight deletion across `pet.py`, `core.py`, and `progress.py`.

**Tech Stack:** Python 3.9+, stdlib only (subprocess, pathlib, json, dataclasses). No new dependencies. No test framework — manual verification per project convention.

**Spec:** `docs/superpowers/specs/2026-04-08-branch-worktree-display-design.md` (commit `ee087ae`). The spec covers the branch/worktree feature only. The icon cleanup (Task 7) is a sibling change requested by the user after the spec was approved; it has no separate spec because it's a straight deletion of dead-end UX with no design choices to make.

**Verification convention:** This project has no test suite. Each task ends with one or more runnable shell commands that exercise the change end-to-end, plus the expected output. If a verification command produces something different, stop and diagnose before moving on.

---

## Pre-flight: Working tree state

The working tree currently has unstaged modifications to `src/claude_statusbar/cli.py`, `src/claude_statusbar/core.py`, and `src/claude_statusbar/progress.py` that implement an in-flight "effort label" feature (adds `--pet` opt-in flag, `get_effort_label()`, and an `effort` kwarg threaded through `format_status_line` at three call sites). These three files are also the files this plan modifies.

The plan **assumes those unstaged changes are committed first** so:
- Line numbers in the spec match the working state
- The `effort` kwarg already exists in `format_status_line` when this plan adds `branch` and `worktree` next to it
- Each commit in this plan represents one feature

If the unstaged changes are not yet ready to commit, stop and finish them first (or stash them). Do not proceed with this plan against a dirty tree containing unrelated work.

---

## Task 1: Commit the in-flight effort-label feature

**Files:**
- Modify (already modified, commit only): `src/claude_statusbar/cli.py`
- Modify (already modified, commit only): `src/claude_statusbar/core.py`
- Modify (already modified, commit only): `src/claude_statusbar/progress.py`

- [ ] **Step 1: Confirm the unstaged diff is the effort-label feature and nothing else**

Run:
```bash
git status
git diff src/claude_statusbar/
```

Expected: three files modified. The diff should show:
- `cli.py`: new `--pet` flag, updated `--pet-name` help text, `show_pet` boolean threaded into `statusbar_main`
- `core.py`: new `get_effort_label()` function, new `show_pet` kwarg on `main()`, `effort=get_effort_label(model_id)` passed at three `format_status_line` call sites, pet rendering gated on `show_pet`, one stray blank line added after the version extraction
- `progress.py`: removed `from claude_statusbar import __version__` import, new `effort: str = ""` kwarg on `format_status_line` signature, new effort rendering block (either dim-colored or plain, depending on `use_color`) inserted between the model append and the bypass check

If the diff shows substantively more than this (e.g., unrelated feature work in other areas of these files, or changes to files not listed), stop and ask the user before proceeding. Small cosmetic differences from this description are OK as long as the overall intent matches "add effort label + pet opt-in."

- [ ] **Step 2: Stage and commit the effort feature**

Run:
```bash
git add src/claude_statusbar/cli.py src/claude_statusbar/core.py src/claude_statusbar/progress.py
git commit -m "feat: add effort label and opt-in pet companion flag"
```

Expected: a single commit on `main` containing all three files.

- [ ] **Step 3: Verify clean tree**

Run:
```bash
git status
```

Expected: `nothing to commit, working tree clean`.

---

## Task 2: Extract `cwd` from the stdin payload

**Files:**
- Modify: `src/claude_statusbar/core.py` (in `parse_stdin_data`, near the top of the result-building block)

The status bar already parses the Claude Code stdin JSON but discards `cwd`. Every other change in this plan depends on this field being available, so it must be added first.

- [ ] **Step 1: Add `cwd` extraction**

In `parse_stdin_data`, find the line:
```python
        # Session ID
        result['session_id'] = data.get('session_id', '')
```

Add immediately below it:
```python
        # Working directory (used by git_info for branch/worktree detection)
        result['cwd'] = data.get('cwd', '')
```

- [ ] **Step 2: Verify the field is now extracted**

Run from the repo root:
```bash
cat ~/.cache/claude-statusbar/last_stdin.json | python3 -c "
import json, sys
sys.path.insert(0, 'src')
from claude_statusbar.core import parse_stdin_data
import io
sys.stdin = io.StringIO(sys.stdin.read())
print(parse_stdin_data().get('cwd'))
"
```

Expected: prints the cwd that was in the cached stdin (e.g. `/Users/sash/apps/claude-code-usage-bar`). If it prints an empty string, the cached stdin doesn't include `cwd` — re-run `claude-statusbar` once inside Claude Code to refresh the cache, then retry.

- [ ] **Step 3: Commit**

```bash
git add src/claude_statusbar/core.py
git commit -m "feat(core): extract cwd from stdin payload"
```

---

## Task 3: Create `git_info.py` module

**Files:**
- Create: `src/claude_statusbar/git_info.py`

This is the only new file in the plan. It contains the dataclass, the public `get_git_info` entry point, and the two private helpers (`_run_git`, `_load_default_branch`, `_read_cache`, `_write_cache`). All subprocess interaction lives here so the rest of the codebase stays subprocess-free.

- [ ] **Step 1: Write the full module**

Create `src/claude_statusbar/git_info.py` with this exact content:

```python
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
        worktree: Optional[str] = git_dir.parent.name
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
```

- [ ] **Step 2: Verify the module imports cleanly and rejects bad inputs**

Run:
```bash
python3 -c "
import sys
sys.path.insert(0, 'src')
from claude_statusbar.git_info import get_git_info, GitInfo

# Missing cwd → None
assert get_git_info(None) is None
assert get_git_info('') is None

# Non-existent cwd → None
assert get_git_info('/nonexistent/path/that/does/not/exist') is None

# Non-repo cwd → None
assert get_git_info('/tmp') is None

print('OK: bad-input cases all return None')
"
```

Expected: `OK: bad-input cases all return None`.

- [ ] **Step 3: Verify the module returns expected results in this repo**

Run from the repo root (currently on `main`, which should be the default branch):
```bash
python3 -c "
import sys
sys.path.insert(0, 'src')
from claude_statusbar.git_info import get_git_info
import os
info = get_git_info(os.getcwd())
print('branch=', info.branch)
print('worktree=', info.worktree)
"
```

Expected: `branch= None` and `worktree= None` (you're on the default branch in the main worktree).

- [ ] **Step 4: Verify the default-branch cache file got created**

Run:
```bash
cat ~/.cache/claude-statusbar/default_branches.json
```

Expected: a JSON object with one entry whose key is the absolute path to this repo's `.git` directory and whose value is `main`. Example:
```json
{"/Users/sash/apps/claude-code-usage-bar/.git": "main"}
```

- [ ] **Step 5: Verify branch detection works on a feature branch**

Run:
```bash
git checkout -b plan-smoke-test
python3 -c "
import sys
sys.path.insert(0, 'src')
from claude_statusbar.git_info import get_git_info
import os
info = get_git_info(os.getcwd())
print('branch=', info.branch)
print('worktree=', info.worktree)
"
git checkout main
git branch -D plan-smoke-test
```

Expected: `branch= plan-smoke-test`, `worktree= None`. The branch token would render. The branch is deleted at the end so this doesn't pollute the repo.

- [ ] **Step 6: Verify worktree detection works**

Run:
```bash
git worktree add /tmp/cs-wt-smoke -b cs-wt-smoke-branch
python3 -c "
import sys
sys.path.insert(0, 'src')
from claude_statusbar.git_info import get_git_info
info = get_git_info('/tmp/cs-wt-smoke')
print('branch=', info.branch)
print('worktree=', info.worktree)
"
git worktree remove /tmp/cs-wt-smoke
git branch -D cs-wt-smoke-branch
```

Expected: `branch= cs-wt-smoke-branch`, `worktree= cs-wt-smoke`. Both tokens would render.

- [ ] **Step 7: Commit**

```bash
git add src/claude_statusbar/git_info.py
git commit -m "feat: add git_info module for branch and worktree detection"
```

---

## Task 4: Add `branch` and `worktree` kwargs to `format_status_line`

**Files:**
- Modify: `src/claude_statusbar/progress.py` (signature and parts assembly inside `format_status_line`)

This task only adds optional rendering. With default-`None` kwargs, every existing call site still works unchanged. The actual wiring happens in Task 5.

- [ ] **Step 1: Add the new kwargs to the signature**

Find this signature near the bottom of `progress.py`:

```python
def format_status_line(
    msgs_pct: Optional[float],
    tkns_pct: Optional[float],
    reset_time: str,
    model: str,
    weekly_pct: Optional[float] = None,
    reset_time_7d: str = "",
    ctx_pct: Optional[float] = None,
    bypass: bool = False,
    use_color: bool = True,
    pet_text: str = "",
    countdown_emoji: str = "",
    effort: str = "",
) -> str:
```

Replace it with:

```python
def format_status_line(
    msgs_pct: Optional[float],
    tkns_pct: Optional[float],
    reset_time: str,
    model: str,
    weekly_pct: Optional[float] = None,
    reset_time_7d: str = "",
    ctx_pct: Optional[float] = None,
    bypass: bool = False,
    use_color: bool = True,
    pet_text: str = "",
    countdown_emoji: str = "",
    effort: str = "",
    branch: Optional[str] = None,
    worktree: Optional[str] = None,
) -> str:
```

- [ ] **Step 2: Insert the rendering between `dim_7d` and `model`**

Find the block in `format_status_line` that looks like this:

```python
    # 7d dimension with its reset time
    dim_7d = _build_dimension("7d", weekly_pct, overall_color, use_color)
    if reset_time_7d:
        dim_7d += colorize(f"⏰{reset_time_7d}", overall_color, use_color)
    parts.append(dim_7d)
    parts.append(colorize(model, overall_color, use_color))
```

Replace it with:

```python
    # 7d dimension with its reset time
    dim_7d = _build_dimension("7d", weekly_pct, overall_color, use_color)
    if reset_time_7d:
        dim_7d += colorize(f"⏰{reset_time_7d}", overall_color, use_color)
    parts.append(dim_7d)
    if branch:
        parts.append(colorize(f" {branch}", overall_color, use_color))
    if worktree:
        parts.append(colorize(f"⎇ {worktree}", overall_color, use_color))
    parts.append(colorize(model, overall_color, use_color))
```

The icon characters are `` (Nerd-Font branch glyph, U+E0A0) and `⎇` (U+2387). Both render in monospace fonts that ship with most terminal emulators.

- [ ] **Step 3: Verify the function still renders correctly without branch/worktree**

Run:
```bash
python3 -c "
import sys
sys.path.insert(0, 'src')
from claude_statusbar.progress import format_status_line
out = format_status_line(
    msgs_pct=30.0, tkns_pct=None, reset_time='3h12m', model='Opus 4.6',
    weekly_pct=20.0, reset_time_7d='2d14h', use_color=False,
)
print(out)
assert 'Opus 4.6' in out
assert '' not in out
assert '⎇' not in out
print('OK: no tokens rendered when branch/worktree are None')
"
```

Expected: a status line with no branch/worktree tokens, ending with `OK: ...`.

- [ ] **Step 4: Verify branch token renders when set**

Run:
```bash
python3 -c "
import sys
sys.path.insert(0, 'src')
from claude_statusbar.progress import format_status_line
out = format_status_line(
    msgs_pct=30.0, tkns_pct=None, reset_time='3h12m', model='Opus 4.6',
    weekly_pct=20.0, reset_time_7d='2d14h', use_color=False,
    branch='feature-x',
)
print(out)
assert ' feature-x' in out
assert '⎇' not in out
print('OK: branch token rendered, worktree absent')
"
```

Expected: a status line including ` feature-x` between the 7d bar and `Opus 4.6`.

- [ ] **Step 5: Verify worktree token renders when set**

Run:
```bash
python3 -c "
import sys
sys.path.insert(0, 'src')
from claude_statusbar.progress import format_status_line
out = format_status_line(
    msgs_pct=30.0, tkns_pct=None, reset_time='3h12m', model='Opus 4.6',
    weekly_pct=20.0, reset_time_7d='2d14h', use_color=False,
    branch='feature-x', worktree='wt-test',
)
print(out)
branch_token = ' feature-x'
worktree_token = '⎇ wt-test'
model_token = 'Opus 4.6'
assert branch_token in out, f'missing branch token: {out!r}'
assert worktree_token in out, f'missing worktree token: {out!r}'
assert model_token in out, f'missing model token: {out!r}'
# Check ordering: branch before worktree before model
i_branch = out.index(branch_token)
i_wt = out.index(worktree_token)
i_model = out.index(model_token)
assert i_branch < i_wt < i_model, f'wrong order: {i_branch} {i_wt} {i_model}'
print('OK: branch and worktree both rendered, in the right order')
"
```

Expected: both tokens present, branch before worktree before model.

- [ ] **Step 6: Commit**

```bash
git add src/claude_statusbar/progress.py
git commit -m "feat(progress): add branch and worktree kwargs to format_status_line"
```

---

## Task 5: Wire `git_info` into `core.main()`

**Files:**
- Modify: `src/claude_statusbar/core.py` (`main()` body — three call sites to `format_status_line`)

This task threads the `git_info` result through the three render paths in `core.main`. The new `show_git` kwarg is added now even though the CLI doesn't yet expose it; Task 6 adds the CLI flag.

- [ ] **Step 1: Add `show_git` kwarg to `main()` and call `get_git_info` once**

Find the `main` function definition near the bottom of `core.py`:

```python
def main(json_output: bool = False,
         reset_hour: Optional[int] = None, use_color: bool = True,
         detail: bool = False, pet_name: Optional[str] = None,
         show_pet: bool = False):
    """Main function"""
    from .pet import format_pet, get_countdown_emoji
    stdin_data = parse_stdin_data()
```

Replace it with:

```python
def main(json_output: bool = False,
         reset_hour: Optional[int] = None, use_color: bool = True,
         detail: bool = False, pet_name: Optional[str] = None,
         show_pet: bool = False, show_git: bool = True):
    """Main function"""
    from .pet import format_pet, get_countdown_emoji
    stdin_data = parse_stdin_data()

    # Resolve git branch/worktree once per render. Deferred import keeps
    # the cold path free of git_info when the user opts out.
    if show_git:
        from .git_info import get_git_info
        git_info_result = get_git_info(stdin_data.get('cwd'))
    else:
        git_info_result = None
    git_branch = git_info_result.branch if git_info_result else None
    git_worktree = git_info_result.worktree if git_info_result else None
```

- [ ] **Step 2: Pass `branch` and `worktree` into the official-data `format_status_line` call**

Find this call (the official-data branch, currently around line 841):

```python
                print(format_status_line(
                    msgs_pct=msgs_pct, tkns_pct=None,
                    reset_time=reset_time, model=model,
                    weekly_pct=weekly_pct,
                    reset_time_7d=reset_time_7d,
                    bypass=bypass, use_color=use_color,
                    pet_text=pet_text, countdown_emoji=countdown,
                    effort=get_effort_label(model_id),
                ))
```

Replace it with:

```python
                print(format_status_line(
                    msgs_pct=msgs_pct, tkns_pct=None,
                    reset_time=reset_time, model=model,
                    weekly_pct=weekly_pct,
                    reset_time_7d=reset_time_7d,
                    bypass=bypass, use_color=use_color,
                    pet_text=pet_text, countdown_emoji=countdown,
                    effort=get_effort_label(model_id),
                    branch=git_branch, worktree=git_worktree,
                ))
```

- [ ] **Step 3: Pass `branch` and `worktree` into the waiting-for-rate-limits `format_status_line` call**

Find this call (currently around line 878):

```python
                    print(format_status_line(
                        msgs_pct=None, tkns_pct=None,
                        reset_time="--", model=model,
                        weekly_pct=None,
                        bypass=bypass, use_color=use_color,
                        pet_text=pet_text,
                        effort=get_effort_label(model_id),
                    ))
```

Replace it with:

```python
                    print(format_status_line(
                        msgs_pct=None, tkns_pct=None,
                        reset_time="--", model=model,
                        weekly_pct=None,
                        bypass=bypass, use_color=use_color,
                        pet_text=pet_text,
                        effort=get_effort_label(model_id),
                        branch=git_branch, worktree=git_worktree,
                    ))
```

- [ ] **Step 4: Leave the error-fallback `format_status_line` call alone — but verify**

Find the call inside the outer `except Exception as e:` block (currently around line 907):

```python
            print(format_status_line(
                msgs_pct=None, tkns_pct=None,
                reset_time=reset_time, model=display_name,
                weekly_pct=None,
                bypass=bypass, use_color=use_color,
                pet_text=pet_text,
                effort=get_effort_label(model_id),
            ))
```

Per the spec, the error path passes neither `branch` nor `worktree`. Confirm this call is **not modified** in this task. The default-`None` kwargs from Task 4 keep it valid.

- [ ] **Step 5: Verify end-to-end render against the cached stdin payload**

Run:
```bash
cat ~/.cache/claude-statusbar/last_stdin.json | python3 -m claude_statusbar.cli
```

Expected: a status line that renders normally. Since you're on `main` (the default branch) in the main worktree, **no branch or worktree token should appear**. The line should look essentially identical to before this plan started.

- [ ] **Step 6: Verify branch token appears on a feature branch**

Run:
```bash
git checkout -b plan-smoke-test
cat ~/.cache/claude-statusbar/last_stdin.json | python3 -m claude_statusbar.cli
git checkout main
git branch -D plan-smoke-test
```

Expected: the rendered line includes ` plan-smoke-test` between the 7d bar and `Opus 4.6`.

- [ ] **Step 7: Commit**

```bash
git add src/claude_statusbar/core.py
git commit -m "feat(core): wire branch and worktree into status line render"
```

---

## Task 6: Add `--no-git` CLI flag

**Files:**
- Modify: `src/claude_statusbar/cli.py` (argparse setup and `statusbar_main` call)

This task exposes the opt-out. After this task, users can suppress branch/worktree display via `--no-git` or `CLAUDE_STATUSBAR_NO_GIT=1`.

- [ ] **Step 1: Add the `--no-git` argument to the parser**

Find this block in `cli.py`:

```python
    parser.add_argument(
        "--no-auto-update",
        action="store_true",
        help="Disable automatic update checks (or set CLAUDE_STATUSBAR_NO_UPDATE=1)",
    )
    parser.add_argument(
        "--pet",
        action="store_true",
        help="Enable the ASCII pet companion (disabled by default, or set CLAUDE_PET=1)",
    )
```

Insert a new argument between them, so the result is:

```python
    parser.add_argument(
        "--no-auto-update",
        action="store_true",
        help="Disable automatic update checks (or set CLAUDE_STATUSBAR_NO_UPDATE=1)",
    )
    parser.add_argument(
        "--no-git",
        action="store_true",
        help="Disable git branch/worktree display in the status bar (or set CLAUDE_STATUSBAR_NO_GIT=1)",
    )
    parser.add_argument(
        "--pet",
        action="store_true",
        help="Enable the ASCII pet companion (disabled by default, or set CLAUDE_PET=1)",
    )
```

- [ ] **Step 2: Compute `show_git` and pass it to `statusbar_main`**

Find this block near the bottom of `main()` in `cli.py`:

```python
    # Run the status bar
    use_color = not (args.no_color or env_bool("NO_COLOR"))
    try:
        show_pet = args.pet or args.pet_name or env_bool("CLAUDE_PET")
        pet_name = args.pet_name or os.environ.get("CLAUDE_PET_NAME") or None
        statusbar_main(json_output=json_output, reset_hour=reset_hour,
                        use_color=use_color, detail=args.detail,
                        pet_name=pet_name, show_pet=show_pet)
        return 0
```

Replace it with:

```python
    # Run the status bar
    use_color = not (args.no_color or env_bool("NO_COLOR"))
    try:
        show_pet = args.pet or args.pet_name or env_bool("CLAUDE_PET")
        pet_name = args.pet_name or os.environ.get("CLAUDE_PET_NAME") or None
        show_git = not (args.no_git or env_bool("CLAUDE_STATUSBAR_NO_GIT"))
        statusbar_main(json_output=json_output, reset_hour=reset_hour,
                        use_color=use_color, detail=args.detail,
                        pet_name=pet_name, show_pet=show_pet,
                        show_git=show_git)
        return 0
```

- [ ] **Step 3: Verify the flag exists in `--help` output**

Run:
```bash
python3 -m claude_statusbar.cli --help | grep -A1 'no-git'
```

Expected: shows `--no-git` and its help text.

- [ ] **Step 4: Verify the flag suppresses branch/worktree on a feature branch**

Run:
```bash
git checkout -b plan-smoke-test
echo "--- without flag ---"
cat ~/.cache/claude-statusbar/last_stdin.json | python3 -m claude_statusbar.cli
echo ""
echo "--- with --no-git ---"
cat ~/.cache/claude-statusbar/last_stdin.json | python3 -m claude_statusbar.cli --no-git
echo ""
echo "--- with env var ---"
cat ~/.cache/claude-statusbar/last_stdin.json | CLAUDE_STATUSBAR_NO_GIT=1 python3 -m claude_statusbar.cli
git checkout main
git branch -D plan-smoke-test
```

Expected: the first line includes ` plan-smoke-test`, the next two lines do not.

- [ ] **Step 5: Commit**

```bash
git add src/claude_statusbar/cli.py
git commit -m "feat(cli): add --no-git flag to suppress branch/worktree display"
```

---

## Task 7: Remove `⏰` alarm prefix and `get_countdown_emoji` system

**Files:**
- Modify: `src/claude_statusbar/pet.py` (delete `get_countdown_emoji`)
- Modify: `src/claude_statusbar/core.py` (drop import, drop call, drop kwarg)
- Modify: `src/claude_statusbar/progress.py` (drop `countdown_emoji` kwarg, drop `⏰` prefix on both bars)

The `⏰` alarm-clock prefix and the `get_countdown_emoji` system (which returns `🎉`/`✨`/`⚡` based on time-to-reset) are being removed in this plan. After this task, reset times render as bare `3h12m` text on each bar with no leading icon and no trailing countdown emoji.

- [ ] **Step 1: Delete `get_countdown_emoji` from `pet.py`**

Find this block at the bottom of `src/claude_statusbar/pet.py` (around line 118):

```python
def get_countdown_emoji(minutes_to_reset: Optional[int]) -> str:
    """Get countdown emoji based on proximity to reset.

    Returns empty string when not in countdown range.
    """
    if minutes_to_reset is None:
        return ""
    if minutes_to_reset <= 1:
        return " \U0001f389"  # party popper
    if minutes_to_reset <= 10:
        return " \u2728"  # sparkles
    if minutes_to_reset <= 30:
        return " \u26a1"  # lightning
    return ""
```

Delete the entire function. If this leaves a trailing blank line at the end of the file, leave a single newline at EOF (the file should still end with one newline).

- [ ] **Step 2: Drop the import in `core.py`**

Find this line in `core.py` (inside `main()`, around line 760):

```python
    from .pet import format_pet, get_countdown_emoji
```

Replace with:

```python
    from .pet import format_pet
```

- [ ] **Step 3: Drop the `countdown` computation and kwarg in `core.py`**

Find this block in the official-data branch of `main()` (around line 836–847):

```python
                pet_pct = msgs_pct if msgs_pct is not None else 0
                pet_text = format_pet(pet_pct, current_hour, session_id,
                                      minutes_to_reset, pet_name) if show_pet else ""
                countdown = get_countdown_emoji(minutes_to_reset)

                print(format_status_line(
                    msgs_pct=msgs_pct, tkns_pct=None,
                    reset_time=reset_time, model=model,
                    weekly_pct=weekly_pct,
                    reset_time_7d=reset_time_7d,
                    bypass=bypass, use_color=use_color,
                    pet_text=pet_text, countdown_emoji=countdown,
                    effort=get_effort_label(model_id),
                    branch=git_branch, worktree=git_worktree,
                ))
```

Replace with:

```python
                pet_pct = msgs_pct if msgs_pct is not None else 0
                pet_text = format_pet(pet_pct, current_hour, session_id,
                                      minutes_to_reset, pet_name) if show_pet else ""

                print(format_status_line(
                    msgs_pct=msgs_pct, tkns_pct=None,
                    reset_time=reset_time, model=model,
                    weekly_pct=weekly_pct,
                    reset_time_7d=reset_time_7d,
                    bypass=bypass, use_color=use_color,
                    pet_text=pet_text,
                    effort=get_effort_label(model_id),
                    branch=git_branch, worktree=git_worktree,
                ))
```

The waiting-for-rate-limits and error-fallback `format_status_line` calls in `core.py` do not pass `countdown_emoji`, so they need no changes here.

- [ ] **Step 4: Drop the `countdown_emoji` parameter from `format_status_line`**

Find the signature in `progress.py`:

```python
def format_status_line(
    msgs_pct: Optional[float],
    tkns_pct: Optional[float],
    reset_time: str,
    model: str,
    weekly_pct: Optional[float] = None,
    reset_time_7d: str = "",
    ctx_pct: Optional[float] = None,
    bypass: bool = False,
    use_color: bool = True,
    pet_text: str = "",
    countdown_emoji: str = "",
    effort: str = "",
    branch: Optional[str] = None,
    worktree: Optional[str] = None,
) -> str:
```

Replace with (the `countdown_emoji` line removed):

```python
def format_status_line(
    msgs_pct: Optional[float],
    tkns_pct: Optional[float],
    reset_time: str,
    model: str,
    weekly_pct: Optional[float] = None,
    reset_time_7d: str = "",
    ctx_pct: Optional[float] = None,
    bypass: bool = False,
    use_color: bool = True,
    pet_text: str = "",
    effort: str = "",
    branch: Optional[str] = None,
    worktree: Optional[str] = None,
) -> str:
```

- [ ] **Step 5: Drop the `⏰` prefix and `countdown_emoji` interpolation from the bar rendering**

Find this block in `format_status_line`:

```python
    # 5h dimension with its reset time + countdown emoji
    dim_5h = _build_dimension("5h", msgs_pct, overall_color, use_color)
    dim_5h += colorize(f"⏰{reset_time}{countdown_emoji}", overall_color, use_color)
    parts = [dim_5h]

    # 7d dimension with its reset time
    dim_7d = _build_dimension("7d", weekly_pct, overall_color, use_color)
    if reset_time_7d:
        dim_7d += colorize(f"⏰{reset_time_7d}", overall_color, use_color)
    parts.append(dim_7d)
```

Replace with:

```python
    # 5h dimension with its reset time
    dim_5h = _build_dimension("5h", msgs_pct, overall_color, use_color)
    dim_5h += colorize(reset_time, overall_color, use_color)
    parts = [dim_5h]

    # 7d dimension with its reset time
    dim_7d = _build_dimension("7d", weekly_pct, overall_color, use_color)
    if reset_time_7d:
        dim_7d += colorize(reset_time_7d, overall_color, use_color)
    parts.append(dim_7d)
```

- [ ] **Step 6: Verify the imports and call sites are clean**

Run:
```bash
grep -rEn 'countdown_emoji|get_countdown_emoji|⏰|🎉|U0001f389|u2728|u26a1' src/claude_statusbar/
```

Expected: zero matches in any source file (`pet.py`, `core.py`, `progress.py`, etc.). If any matches remain, locate and remove them before continuing. (The `egg-info/PKG-INFO` file is auto-generated and not in `src/claude_statusbar/`; if it shows in unrelated greps, ignore it.)

Note: The `-E` flag is required so that `|` is treated as alternation on BSD grep (macOS). GNU grep accepts either form, but BSD grep treats `\|` as a literal `\|` and returns no matches — silently turning this verification into a no-op.

- [ ] **Step 7: Verify the bar still renders end-to-end**

Run:
```bash
cat ~/.cache/claude-statusbar/last_stdin.json | python3 -m claude_statusbar.cli
```

Expected: a status line that renders normally. The reset time on the 5h bar appears as bare text (e.g. `3h12m`) immediately after the closing bracket of the bar, with no `⏰` prefix and no trailing emoji. Same for the 7d bar.

- [ ] **Step 8: Verify nothing crashes when reset times are present and varied**

Run:
```bash
python3 -c "
import sys
sys.path.insert(0, 'src')
from claude_statusbar.progress import format_status_line
# Common case
out = format_status_line(
    msgs_pct=30.0, tkns_pct=None, reset_time='3h12m', model='Opus 4.6',
    weekly_pct=20.0, reset_time_7d='2d14h', use_color=False,
)
print(out)
assert '⏰' not in out
assert '🎉' not in out
assert '✨' not in out
assert '⚡' not in out
assert '3h12m' in out
assert '2d14h' in out
print('OK: no icons, reset times still rendered')
"
```

Expected: status line with `3h12m` and `2d14h` present, none of the removed icons present.

- [ ] **Step 9: Commit**

```bash
git add src/claude_statusbar/pet.py src/claude_statusbar/core.py src/claude_statusbar/progress.py
git commit -m "refactor: remove alarm icon and countdown emoji system"
```

---

## Task 8: Manual verification sweep

**Files:** none modified — runs the full verification matrix from the spec.

This task executes every verification step from the spec's Testing section. Stop and diagnose if any check fails.

- [ ] **Step 1: Default branch in main repo → no tokens, no removed icons**

Run from the repo root (currently on `main`):
```bash
cat ~/.cache/claude-statusbar/last_stdin.json | python3 -m claude_statusbar.cli
```

Expected: status line ends `... | Opus 4.6 | <effort> | <version>` with **no** branch or worktree token between the 7d bar and the model. Reset times appear as bare text (`3h12m`, `2d14h`) on each bar with **no** `⏰` prefix and **no** countdown emoji (`🎉`/`✨`/`⚡`).

- [ ] **Step 2: Feature branch → branch token**

```bash
git checkout -b plan-smoke-test
cat ~/.cache/claude-statusbar/last_stdin.json | python3 -m claude_statusbar.cli
```

Expected: ` plan-smoke-test` token appears between the 7d bar and the model. No worktree token.

- [ ] **Step 3: Secondary worktree → both tokens**

The git probe uses the `cwd` field from the stdin payload, not the shell's cwd. To exercise the worktree, we patch the stdin payload to point at the worktree path:

```bash
git worktree add /tmp/cs-wt-smoke plan-smoke-test
python3 -c "
import json, sys, io
payload = json.load(open('/Users/sash/.cache/claude-statusbar/last_stdin.json'))
payload['cwd'] = '/tmp/cs-wt-smoke'
sys.stdin = io.StringIO(json.dumps(payload))
from claude_statusbar.cli import main
sys.exit(main())
"
```

Expected: both ` plan-smoke-test` and `⎇ cs-wt-smoke` tokens appear in the rendered line, branch before worktree, both before the model name.

- [ ] **Step 4: Cleanup the worktree and feature branch**

```bash
git worktree remove /tmp/cs-wt-smoke
git checkout main
git branch -D plan-smoke-test
```

Expected: clean working tree, back on `main`.

- [ ] **Step 5: Non-repo cwd → no tokens, no error**

```bash
python3 -c "
import json, sys, io
payload = json.load(open('/Users/sash/.cache/claude-statusbar/last_stdin.json'))
payload['cwd'] = '/tmp'
sys.stdin = io.StringIO(json.dumps(payload))
from claude_statusbar.cli import main
sys.exit(main())
"
```

Expected: status line renders without branch/worktree tokens. No errors on stderr.

- [ ] **Step 6: Opt-out via `--no-git` and env var**

```bash
git checkout -b plan-smoke-test
echo "--- with --no-git ---"
cat ~/.cache/claude-statusbar/last_stdin.json | python3 -m claude_statusbar.cli --no-git
echo "--- with CLAUDE_STATUSBAR_NO_GIT=1 ---"
cat ~/.cache/claude-statusbar/last_stdin.json | CLAUDE_STATUSBAR_NO_GIT=1 python3 -m claude_statusbar.cli
git checkout main
git branch -D plan-smoke-test
```

Expected: neither line includes ` plan-smoke-test`.

- [ ] **Step 7: Bare repo at cwd**

```bash
git clone --bare https://github.com/octocat/Hello-World /tmp/cs-bare-test.git 2>/dev/null || git init --bare /tmp/cs-bare-test.git
python3 -c "
import json, sys, io
payload = json.load(open('/Users/sash/.cache/claude-statusbar/last_stdin.json'))
payload['cwd'] = '/tmp/cs-bare-test.git'
sys.stdin = io.StringIO(json.dumps(payload))
from claude_statusbar.cli import main
sys.exit(main())
"
rm -rf /tmp/cs-bare-test.git
```

Expected: status line renders without crashing. No errors on stderr. (Bare repo HEAD state may or may not match a default branch — either no token or a branch token is acceptable as long as nothing crashes.)

- [ ] **Step 8: Detached HEAD**

First confirm the working tree is clean — `git checkout HEAD~1` with uncommitted changes in tracked files will either refuse (safe) or silently carry changes forward (less safe):
```bash
git status --porcelain
```

Expected: empty output. If it prints anything, stash or commit before proceeding so this smoke test doesn't perturb unrelated work.

Then:
```bash
git checkout HEAD~1
cat ~/.cache/claude-statusbar/last_stdin.json | python3 -m claude_statusbar.cli
git checkout main
```

Expected: a ` <short-sha>` token appears (e.g. ` ee087ae`) since detached HEAD is always non-default.

- [ ] **Step 9: Default-branch cache cold/warm**

```bash
rm -f ~/.cache/claude-statusbar/default_branches.json
echo "--- cold (cache miss, runs git symbolic-ref) ---"
time (cat ~/.cache/claude-statusbar/last_stdin.json | python3 -m claude_statusbar.cli > /dev/null)
echo "--- warm (cache hit, no symbolic-ref) ---"
time (cat ~/.cache/claude-statusbar/last_stdin.json | python3 -m claude_statusbar.cli > /dev/null)
echo "--- cache file contents ---"
cat ~/.cache/claude-statusbar/default_branches.json
```

Expected: both runs render the status line correctly. The cache file is created and contains one entry whose key is the absolute path to this repo's `.git` directory and whose value is `main`.

The warm run *should* be slightly faster in theory (one fewer subprocess), but on modern hardware the difference is a few milliseconds against Python interpreter startup noise of 30–100 ms, so a single `time` comparison is unreliable as a pass/fail signal. Treat the timing as informational only — the real check is that the cache file exists with the expected contents after the first run.

- [ ] **Step 10: Git not on PATH (skip if too inconvenient)**

This is hard to simulate cleanly without modifying PATH. The trick is to put git out of reach of the Python subprocess **without** also hiding the tools the rest of the command needs. The cleanest way is to temporarily shadow `git` with an empty directory added to the front of `PATH`:

```bash
mkdir -p /tmp/cs-empty-path
cat ~/.cache/claude-statusbar/last_stdin.json | \
  env PATH=/tmp/cs-empty-path:/nonexistent python3 -m claude_statusbar.cli
rmdir /tmp/cs-empty-path
```

The key detail is that `PATH=/tmp/cs-empty-path:/nonexistent` contains no `git` binary anywhere, so `subprocess.run(["git", ...])` raises `FileNotFoundError`, which `_run_git` catches. Python itself is invoked via its full path because the parent shell already resolved `python3` before `env` ran.

Expected: status line renders without branch/worktree tokens. No errors on stderr. (Note: do **not** use `PATH=/usr/bin` — that varies by OS, and on macOS `/usr/bin/git` is Xcode's git shim, which is still a working git binary.)

- [ ] **Step 11: No commit needed for this task**

Nothing was modified in Task 8. Move on to Task 9.

---

## Task 9: Bump version

**Files:**
- Modify: `src/claude_statusbar/__init__.py` (version bump)

The project bumps version on every shipped feature (the most recent commits include `chore: bump version to 2.4.3`). This task bumps to the next patch and commits.

- [ ] **Step 1: Read the current version**

Run:
```bash
grep -n '__version__' src/claude_statusbar/__init__.py
```

Expected: a single line like `__version__ = "2.4.3"`. Note the current version.

- [ ] **Step 2: Bump the patch version**

If the current version is `2.4.3`, edit `src/claude_statusbar/__init__.py` to change:
```python
__version__ = "2.4.3"
```
to:
```python
__version__ = "2.4.4"
```

If the current version is something else, bump the patch by one (e.g., `2.5.1` → `2.5.2`).

- [ ] **Step 3: Verify the bump**

Run:
```bash
python3 -m claude_statusbar.cli --version
```

Expected: prints the new version.

- [ ] **Step 4: Commit**

```bash
git add src/claude_statusbar/__init__.py
git commit -m "chore: bump version for branch/worktree display and icon cleanup"
```

- [ ] **Step 5: Final tree check**

Run:
```bash
git status
git log --oneline -12
```

Expected: clean working tree. The recent log should include (most recent first) roughly the following commits, in this order relative to each other:

1. `chore: bump version for branch/worktree display and icon cleanup`
2. `refactor: remove alarm icon and countdown emoji system`
3. `feat(cli): add --no-git flag to suppress branch/worktree display`
4. `feat(core): wire branch and worktree into status line render`
5. `feat(progress): add branch and worktree kwargs to format_status_line`
6. `feat: add git_info module for branch and worktree detection`
7. `feat(core): extract cwd from stdin payload`
8. `feat: add effort label and opt-in pet companion flag`

Earlier plan-related commits (the spec, the plan itself, review-feedback commits on either) may appear below these but the exact set depends on how the plan was landed — don't block on their presence. The important signal is that the 8 feature commits above are present, in order, at the top of the log.

---

## Done

After Task 9, both features are complete:
- The status bar now shows ` <branch>` when on a non-default branch and `⎇ <worktree>` when in a secondary worktree, with `--no-git` / `CLAUDE_STATUSBAR_NO_GIT=1` as the opt-out. Default-branch lookups are cached forever per repo.
- Reset times render as bare text on each bar (no `⏰` prefix, no `🎉`/`✨`/`⚡` countdown emoji).

If you discover that the feature should be re-installed for use in your active Claude Code statusline, the project's prior memory note recommends:
```bash
uv tool install --force -e .
```
from the repo root. Run this only after Task 8 completes.
