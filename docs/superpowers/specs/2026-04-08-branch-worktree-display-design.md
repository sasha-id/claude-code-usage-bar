# Branch and Worktree Display

## Goal

Surface git branch and worktree information in the status bar — but only when they convey something meaningful. The current branch is shown only if it differs from the repo's default branch. The worktree name is shown only when running inside a secondary worktree.

## Motivation

When working across many repos and worktrees in Claude Code, the only constant in the status bar is the model name. The user often loses track of which branch and worktree the session is operating in. Always-on branch display would clutter the bar in the common case (default branch, primary worktree); conditional display gives signal without noise.

## User-Visible Behavior

The status bar gains up to two new tokens, positioned between the 7-day rate-limit bar and the model name:

```
5h[██   30%]⏰3h12m | 7d[█    20%]⏰2d14h |  feature-x | ⎇ wt-test | Opus 4.6 | high/max
```

Rules:

- ` <branch>` appears only when the current branch differs from the repo's default branch.
- `⎇ <worktree>` appears only when `git rev-parse --git-dir` differs from `git rev-parse --git-common-dir` (i.e., a secondary worktree). The worktree name is the directory name under `.git/worktrees/`.
- When the cwd is not inside a git repository, neither token appears.
- When git is unavailable or any git command fails, neither token appears. The bar continues to render normally.
- Tokens use the same `overall_color` as the surrounding bar so they stay visually consistent.

## Architecture

A single new module owns all git interaction. The rest of the codebase consumes its result through a small dataclass.

```
stdin.cwd ──> git_info.get_git_info(cwd) ──> GitInfo(branch, worktree)
                      │
                      ├── git rev-parse --git-dir --git-common-dir
                      │                  --show-toplevel --abbrev-ref HEAD
                      │                  (one subprocess, four answers)
                      │
                      └── default_branches.json (lookup or git symbolic-ref)
                              │
                              ▼
                format_status_line(..., branch=, worktree=)
```

`core.py` calls `get_git_info(stdin_data.get('cwd'))` once after parsing stdin and threads the result into `format_status_line`. `progress.py` renders the tokens.

## Components

### `src/claude_statusbar/git_info.py` (new)

```python
@dataclass(frozen=True)
class GitInfo:
    branch: str | None       # None means "default branch, hide token"
    worktree: str | None     # None means "main worktree, hide token"

def get_git_info(cwd: str | None) -> GitInfo | None:
    """Return git info for cwd, or None if cwd is missing/not a repo/git unavailable."""
```

Internals:

- One subprocess call: `git -C <cwd> rev-parse --git-dir --git-common-dir --show-toplevel --abbrev-ref HEAD`. Splits output into four lines. 200 ms timeout.
- If the call fails for any reason, return `None`.
- Worktree name: if `git-dir != git-common-dir`, parse `git-dir` to extract the worktree name (the segment after `worktrees/`). Otherwise `worktree = None`.
- Default branch: call `_load_default_branch(toplevel)`.
- Branch token: if current branch equals the default, `branch = None`; otherwise `branch = current_branch`. Detached HEAD (`abbrev-ref HEAD == "HEAD"`) shows the short SHA from a separate `git rev-parse --short HEAD` call.

### Default-branch cache

- File: `~/.cache/claude-statusbar/default_branches.json`
- Shape: `{ "<toplevel-path>": "<default-branch-name>" }`
- Read once per process. On a miss: run `git -C <toplevel> symbolic-ref --short refs/remotes/origin/HEAD`, store result, write back atomically.
- If the symbolic-ref call fails (no remote, shallow clone, etc.), fall back to checking against the heuristic set `{"main", "master"}`. The fallback result is **not** cached, so a remote added later will start working on the next render.
- Cache is keyed by the absolute toplevel path, so worktrees sharing a repo share the cache entry.

### `src/claude_statusbar/progress.py` (modified)

`format_status_line` gains two new optional kwargs:

```python
def format_status_line(
    ...,
    branch: str | None = None,
    worktree: str | None = None,
) -> str:
```

When `branch` is set, append `" <branch>"` as its own part. When `worktree` is set, append `"⎇ <worktree>"` as its own part. Both are colored with `overall_color` and inserted into `parts` immediately before the model token. Default arguments preserve existing call sites.

### `src/claude_statusbar/core.py` (modified)

Three call sites need the new arguments threaded through:

1. **Official-data branch** (around line 841): call `get_git_info` once after `parse_stdin_data`, pass `branch` and `worktree` into `format_status_line`.
2. **Waiting-for-rate-limits branch** (around line 878): same.
3. **Error fallback** (around line 907): pass `branch=None, worktree=None` so the bar still renders if anything upstream blew up.

The `get_git_info` call happens once per render and the result is shared across both display branches. JSON output mode is unaffected.

## Data Flow

1. Claude Code injects stdin JSON containing `cwd`.
2. `core.main()` parses stdin via `parse_stdin_data()`.
3. `core.main()` calls `git_info.get_git_info(stdin_data.get('cwd'))` once.
4. `get_git_info` runs one `git rev-parse` subprocess and reads/writes the default-branch cache as needed.
5. `core.main()` passes the resulting `branch` and `worktree` strings into `format_status_line`.
6. `format_status_line` renders them as colored tokens between the 7d bar and the model name.

## Error Handling

Every failure mode collapses to "show nothing for this dimension." The status bar must keep rendering exactly as it does today if anything goes wrong.

| Failure | Behavior |
|---|---|
| `cwd` missing from stdin | Return `None`. No tokens. |
| `cwd` is not a directory | Return `None`. No tokens. |
| `git` not on PATH | Return `None`. No tokens. |
| `git rev-parse` exits non-zero (not a repo) | Return `None`. No tokens. |
| Subprocess exceeds 200 ms timeout | Return `None`. No tokens. |
| `git symbolic-ref` for default branch fails | Fall back to `{"main", "master"}` heuristic. |
| Default-branch cache file is corrupt | Treat as empty. Re-resolve and overwrite. |
| Default-branch cache write fails | Silent. Lookup re-runs next render. |

This matches the defensive style used by `parse_stdin_data` and `is_bypass_permissions_active` in `core.py`.

## Performance

- One extra `git rev-parse` per render in the common case: ~5–10 ms.
- One extra `git symbolic-ref` per repo, **once ever**: ~5–10 ms first render in a new repo, 0 ms after.
- One extra `git rev-parse --short HEAD` only when HEAD is detached, in addition to the call above: ~5–10 ms.
- No new threads, background processes, or daemons.
- Steady-state added latency: ~5–10 ms common case, ~10–20 ms when HEAD is detached or on the very first render in a new repo.

The status bar already runs `claude-monitor` subprocesses for rate-limit data, so this added cost is well below the existing budget.

## Testing

Manual verification, matching the existing project pattern (no `tests/` directory yet).

Verification steps:

1. Run `claude-statusbar` from this repo on the default branch. Expect: no branch or worktree token.
2. `git checkout -b throwaway-test` and re-run. Expect: ` throwaway-test` token, no worktree token.
3. `git worktree add ../wt-smoke throwaway-test` and run from `../wt-smoke`. Expect: both ` throwaway-test` and `⎇ wt-smoke` tokens.
4. Run `claude-statusbar` from `/tmp` (non-repo). Expect: bar renders unchanged with no new tokens.
5. Temporarily rename `git` on PATH or run from a directory without git installed. Expect: bar renders unchanged with no new tokens, no errors on stderr.

## Out of Scope

- Configurable display format (icons, colors, position) — fixed for now.
- Showing ahead/behind counts vs. upstream.
- Showing dirty-tree indicators.
- Showing tag information.
- Per-repo overrides for "what counts as default."
- Caching worktree or branch results — they change too often for caching to help.
- A test suite — the project doesn't have one yet, and adding one is a separate decision.
