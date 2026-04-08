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

- ` <branch>` appears only when the current branch differs from the repo's default branch (resolved via `git symbolic-ref refs/remotes/origin/HEAD`, with a fallback heuristic when that's unavailable).
- `⎇ <worktree>` appears only when `git rev-parse --git-dir` differs from `git rev-parse --git-common-dir` (i.e., a secondary worktree). The worktree name is computed as `Path(git_dir).parent.name`.
- When the cwd is not inside a git repository, neither token appears.
- When git is unavailable or any git command fails, neither token appears. The bar continues to render normally.
- When the user opts out via `--no-git` or `CLAUDE_STATUSBAR_NO_GIT=1`, neither token appears.
- Tokens use the same `overall_color` as the surrounding bar so they stay visually consistent.

## Architecture

A single new module owns all git interaction. The rest of the codebase consumes its result through a small dataclass (`GitInfo`).

```
stdin.cwd ──> git_info.get_git_info(cwd) ──> GitInfo(branch, worktree)
                      │
                      ├── git rev-parse --git-dir --git-common-dir
                      │                  --abbrev-ref HEAD
                      │                  (one subprocess, three answers)
                      │
                      ├── git rev-parse --short HEAD
                      │   (only when HEAD is detached)
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

- One subprocess call: `git -C <cwd> rev-parse --git-dir --git-common-dir --abbrev-ref HEAD`. Splits output into three lines. 200 ms timeout. (`--show-toplevel` is intentionally **not** included — it errors out on bare repos. We use `--git-common-dir` as the per-repo identifier instead, which works for bare repos and naturally shares the cache between worktrees of the same repo.)
- If the call fails for any reason, return `None`.
- Worktree name: if `git-dir != git-common-dir`, the worktree is secondary. Compute the name as `Path(git_dir).parent.name` (the directory name immediately above the worktree's `.git` file — equivalent to the segment after `.git/worktrees/`, but unambiguous even if some other path component happens to contain the string `worktrees`). Otherwise `worktree = None`.
- Default branch: call `_load_default_branch(common_dir_abs)`, where `common_dir_abs` is the absolute path to `--git-common-dir`.
- Branch token: if `current_branch` equals the default, `branch = None`; otherwise `branch = current_branch`. Detached HEAD is detected by the literal sentinel `current_branch == _DETACHED_HEAD_SENTINEL` (a module-level constant equal to `"HEAD"`); in that case run a second subprocess `git -C <cwd> rev-parse --short HEAD` and use the resulting short SHA as the branch label (which is always non-default, so always rendered).

### Default-branch cache

- File: `~/.cache/claude-statusbar/default_branches.json`
- Shape: `{ "<git-common-dir-abs>": "<default-branch-name>" }`
- Read once per `get_git_info` call. On a miss: run `git -C <cwd> symbolic-ref --short refs/remotes/origin/HEAD`, store result, write back atomically using the existing tempfile + rename pattern from `cache.py::write_cache`.
- If the symbolic-ref call fails (no remote, shallow clone, unset `origin/HEAD`, etc.), fall back to checking against the heuristic set `{"main", "master", "develop", "trunk", "staging"}`. The fallback result is **not** cached, so a `git remote add origin` (or equivalent) starts working on the next render without manual cache busting.
- Cache is keyed by the absolute `git-common-dir`, so all worktrees of the same repo share the entry and bare repos work too.
- **Concurrency:** the read-check-write cycle is not locked. Two concurrent renders can both miss the cache, both shell out to `git symbolic-ref`, and both write. This is intentional — the result is idempotent, last write wins, and a lock would serialize renders for no benefit.

### `src/claude_statusbar/progress.py` (modified)

`format_status_line` gains two new optional kwargs:

```python
def format_status_line(
    ...,
    branch: str | None = None,
    worktree: str | None = None,
) -> str:
```

When `branch` is set, append a part with the literal value `" <branch>"` (the icon character `` followed by a single space and then the branch name — no leading whitespace beyond what the existing ` | ` separator provides). When `worktree` is set, append a part with the literal value `"⎇ <worktree>"` (icon, space, name). Both are colored with `overall_color` so they match the rest of the bar.

**Insertion position:** in the existing `parts` list assembly, the order today is `[dim_5h, dim_7d, model, effort?, bypass?, pet?]`. The new tokens go **after `dim_7d` and before `model`**, in this order: `[dim_5h, dim_7d, branch?, worktree?, model, effort?, bypass?, pet?]`. This matches the example in the User-Visible Behavior section. Default kwargs of `None` preserve every existing call site without modification.

### `src/claude_statusbar/core.py` (modified)

Two changes:

**A. Add `cwd` to `parse_stdin_data`.** The function currently does not extract `cwd` from the stdin payload. Add `result['cwd'] = data.get('cwd', '')` near the other top-level field assignments. Without this, every other change in the spec is a no-op.

**B. Thread `branch`/`worktree` through three call sites.** Line numbers are approximate — they'll shift slightly once the in-flight unstaged edits in `cli.py`/`core.py`/`progress.py` get committed before implementation begins.

1. **Official-data branch** (around line 841): call `get_git_info(stdin_data.get('cwd'))` once after parsing stdin, then pass `branch=git_info.branch if git_info else None` and likewise for `worktree` into `format_status_line`. Gate the call on the new opt-out flag (see CLI section below) — when disabled, skip the call entirely and pass `None` for both.
2. **Waiting-for-rate-limits branch** (around line 878): reuse the same `git_info` value computed once at the top of `main()`. Do not call `get_git_info` twice.
3. **Error fallback** (around line 907): pass `branch=None, worktree=None`. If an exception fires after `get_git_info` succeeded, we discard the valid git info — that's deliberate, the error path prioritizes simplicity over completeness.

JSON output mode is intentionally **not** extended with branch/worktree fields. Reasons: (1) the JSON schema is currently stable across statusbar versions and consumed by external scripts, and (2) anything that wants git info from cwd can compute it independently. Adding the fields later is non-breaking; removing them is. This is a deliberate omission, not an oversight.

### `src/claude_statusbar/cli.py` (modified)

Add an opt-out, mirroring the existing `--no-color` / `--no-auto-update` pattern:

- New CLI flag: `--no-git`
- New env var: `CLAUDE_STATUSBAR_NO_GIT=1`
- Both feed a single boolean that's threaded into `core.main()` as a new kwarg `show_git: bool = True`. When `False`, `core.main()` skips the `get_git_info` call entirely and never imports `git_info` (saves a few ms on cold start for users who opt out).
- Help text: `Disable git branch/worktree display in the status bar (or set CLAUDE_STATUSBAR_NO_GIT=1)`.

## Data Flow

1. Claude Code injects stdin JSON containing `cwd`.
2. `core.main()` parses stdin via `parse_stdin_data()`, which now extracts `cwd` into `result['cwd']`.
3. If the opt-out flag is set, skip to step 6 with `git_info = None`.
4. `core.main()` calls `git_info.get_git_info(stdin_data.get('cwd'))` once.
5. `get_git_info` runs one `git rev-parse` subprocess (plus an optional second one for detached HEAD), reads/writes the default-branch cache as needed, and returns a `GitInfo` dataclass (or `None`).
6. `core.main()` passes `git_info.branch` and `git_info.worktree` (or `None`/`None`) into `format_status_line` at every render call site.
7. `format_status_line` renders them as colored tokens between the 7d bar and the model name.

## Error Handling

Every failure mode collapses to "show nothing for this dimension." The status bar must keep rendering exactly as it does today if anything goes wrong.

| Failure | Behavior |
|---|---|
| `cwd` missing from stdin | Return `None`. No tokens. |
| `cwd` is not a directory | Return `None`. No tokens. |
| `git` not on PATH | Return `None`. No tokens. |
| `git rev-parse` exits non-zero (not a repo) | Return `None`. No tokens. |
| Bare repo at `cwd` | Supported. `--git-common-dir` works fine; `--show-toplevel` is intentionally not used. |
| Subprocess exceeds 200 ms timeout | Return `None`. No tokens. |
| Detached HEAD | `current_branch` will be the literal `"HEAD"`; second `git rev-parse --short HEAD` call provides the short SHA, displayed as the branch label. |
| `git symbolic-ref` for default branch fails | Fall back to `{"main", "master", "develop", "trunk", "staging"}` heuristic. Not cached. |
| Default-branch cache file is corrupt | Treat as empty. Re-resolve and overwrite. |
| Default-branch cache write fails | Silent. Lookup re-runs next render. |
| Opt-out enabled (`--no-git` or `CLAUDE_STATUSBAR_NO_GIT=1`) | `get_git_info` is never called. No tokens. |

This matches the defensive style used by `parse_stdin_data` and `is_bypass_permissions_active` in `core.py`.

## Performance

- **Common case (named branch, repo seen before, opt-out off):** one extra `git rev-parse` subprocess per render. ~5–10 ms.
- **First render in a new repo:** common case + one extra `git symbolic-ref` to resolve the default branch. ~10–20 ms total. Resolved value is persisted to disk and reused forever.
- **Detached HEAD:** common case + one extra `git rev-parse --short HEAD` to get the SHA. ~10–20 ms total.
- **Opt-out enabled:** zero extra subprocesses, zero extra latency, `git_info` module is not even imported.
- No new threads, background processes, or daemons.

The status bar already runs `claude-monitor` subprocesses for rate-limit data, so this added cost is well below the existing budget.

## Testing

Manual verification, matching the existing project pattern (no `tests/` directory yet).

Verification steps:

1. Run `claude-statusbar` from this repo on the default branch. Expect: no branch or worktree token.
2. `git checkout -b throwaway-test` and re-run. Expect: ` throwaway-test` token, no worktree token.
3. `git worktree add ../wt-smoke throwaway-test` and run from `../wt-smoke`. Expect: both ` throwaway-test` and `⎇ wt-smoke` tokens.
4. Run `claude-statusbar` from `/tmp` (non-repo). Expect: bar renders unchanged with no new tokens.
5. Temporarily rename `git` on PATH or run from a directory without git installed. Expect: bar renders unchanged with no new tokens, no errors on stderr.
6. **Opt-out:** run `CLAUDE_STATUSBAR_NO_GIT=1 claude-statusbar` from a non-default branch. Expect: no branch or worktree token. Run `claude-statusbar --no-git` for the same expectation.
7. **Bare repo:** `git clone --bare <some-repo> /tmp/bare.git && cd /tmp/bare.git && claude-statusbar`. Expect: bar renders without crashing; tokens may or may not appear depending on the bare repo's HEAD state, but no errors on stderr.
8. **Detached HEAD:** `git checkout HEAD~1` (after committing the throwaway branch) and re-run. Expect: ` <short-sha>` token (since detached HEAD is always non-default).
9. **Default-branch cache cold/warm:** delete `~/.cache/claude-statusbar/default_branches.json` and run twice. The second run should be measurably faster (one fewer subprocess) and the cache file should reappear with the toplevel-keyed entry.

## Out of Scope

- Configurable display format (icons, colors, position) — fixed for now.
- Showing ahead/behind counts vs. upstream.
- Showing dirty-tree indicators.
- Showing tag information.
- Per-repo overrides for "what counts as default."
- Caching worktree or branch results — they change too often for caching to help.
- A test suite — the project doesn't have one yet, and adding one is a separate decision.
