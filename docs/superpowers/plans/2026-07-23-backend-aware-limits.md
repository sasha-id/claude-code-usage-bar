# Backend-Aware 5h/7d Limits Implementation Plan

> **Execution record:** Implemented and verified on 2026-07-23. The task checkboxes below preserve the original task breakdown.

**Goal:** Show the active backend's real 5h/7d limits in the status bar — Anthropic subscription numbers on the default backend, Kimi coding-plan quota on kimi-k3 — and stop showing stale Anthropic numbers on kimi sessions.

**Architecture:** Runtime changes are in `src/claude_statusbar/core.py`, with focused coverage in the Kimi usage and context-smoothing tests. A `_backend()` helper classifies the session from `ANTHROPIC_BASE_URL`; on the kimi backend a throttled, file-cached GET to `<base>/v1/usages` maps Kimi's 5-hour and weekly windows into the existing `rate_limit_*` result keys, so `progress.py` and everything downstream are untouched. Spec: `docs/superpowers/specs/2026-07-23-backend-aware-limits-design.md`.

**Tech Stack:** Python ≥3.9, stdlib only (`urllib.request`, `json`, `time`, `pathlib`), pytest.

---

## File Structure

- **Modify:** `src/claude_statusbar/core.py` — add `_backend()`, `_fetch_kimi_usage()`, `_kimi_usage()`; restructure the rate-limit block of `parse_stdin_data()`.
- **Create:** `tests/test_kimi_usage.py` — all new tests.
- **Modify:** `tests/test_context_smoothing.py` — cover active-lock ownership and per-session state during stale-lock checks.

Existing test conventions (match them): plain pytest functions, imports like `from claude_statusbar.core import _smooth_context`, `tmp_path` for file state, `monkeypatch.setenv` / `monkeypatch.setattr` for isolation (see `tests/test_context_smoothing.py`, `tests/test_cli.py`).

---

### Task 1: `_backend()` — session backend detection

**Files:**
- Modify: `src/claude_statusbar/core.py` (imports at top, helper after `_smooth_context`)
- Test: `tests/test_kimi_usage.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_kimi_usage.py`:

```python
"""Backend-aware 5h/7d limits: default (Anthropic subscription) vs kimi-k3."""
from claude_statusbar.core import _backend


class TestBackend:
    def test_unset_base_url_is_default(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        assert _backend() == "default"

    def test_empty_base_url_is_default(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "  ")
        assert _backend() == "default"

    def test_kimi_host_is_kimi(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.kimi.com/coding")
        assert _backend() == "kimi"

    def test_kimi_apex_host_is_kimi(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://kimi.com/coding")
        assert _backend() == "kimi"

    def test_lookalike_host_is_not_kimi(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://notkimi.com/v1")
        assert _backend() == "other"

    def test_other_byo_host_is_other(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://litellm.ts.sasha.id")
        assert _backend() == "other"

    def test_unparseable_base_url_is_other(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "not a url")
        assert _backend() == "other"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kimi_usage.py -v`
Expected: FAIL with `ImportError: cannot import name '_backend'`

- [ ] **Step 3: Implement `_backend()`**

In `src/claude_statusbar/core.py`, add the import at the top (with the other stdlib imports):

```python
from urllib.parse import urlparse
```

Add the helper right after `_smooth_context` (before `parse_stdin_data`):

```python
def _backend() -> str:
    """Which credential backend this session runs on, from the env Claude Code
    passes to the status line. The nyx launcher strips ANTHROPIC_BASE_URL on the
    default (nyx auth) path, so absence means the Anthropic subscription; a
    *.kimi.com host is the kimi-k3 byo backend; anything else is a byo host we
    have no usage provider for."""
    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    if not base:
        return "default"
    host = urlparse(base).hostname or ""
    if host == "kimi.com" or host.endswith(".kimi.com"):
        return "kimi"
    return "other"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_kimi_usage.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_statusbar/core.py tests/test_kimi_usage.py
git commit -m "feat: detect session backend (default/kimi/other) from ANTHROPIC_BASE_URL"
```

---

### Task 2: `_fetch_kimi_usage()` — fetch + parse the Kimi usages API

**Files:**
- Modify: `src/claude_statusbar/core.py`
- Test: `tests/test_kimi_usage.py`

The response shape below is the verified live payload from `GET https://api.kimi.com/coding/v1/usages` — weekly quota in the top-level `usage` object, the 5-hour rolling window as a `limits[]` entry with `window.duration == 300` minutes.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_kimi_usage.py`:

```python
import json

import pytest

from claude_statusbar import core
from claude_statusbar.core import _fetch_kimi_usage

# Verified live response shape (2026-07-23), values are strings by design.
LIVE_PAYLOAD = {
    "usage": {"limit": "100", "used": "12", "remaining": "88",
              "resetTime": "2026-07-29T14:51:08.099871Z"},
    "limits": [
        {"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
         "detail": {"limit": "100", "used": "16", "remaining": "84",
                    "resetTime": "2026-07-23T15:51:08.099871Z"}},
    ],
}


class _FakeResp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._raw


def _mock_urlopen(monkeypatch, payload):
    calls = []

    def fake(req, timeout=None):
        calls.append((req, timeout))
        return _FakeResp(payload)

    monkeypatch.setattr(core.urllib.request, "urlopen", fake)
    return calls


class TestFetchKimiUsage:
    def test_parses_both_windows(self, monkeypatch):
        calls = _mock_urlopen(monkeypatch, LIVE_PAYLOAD)
        out = _fetch_kimi_usage("https://api.kimi.com/coding/", "tok")
        assert out["seven_day"]["pct"] == pytest.approx(12.0)
        assert out["five_hour"]["pct"] == pytest.approx(16.0)
        # ISO resetTime converted to epoch seconds
        assert out["seven_day"]["resets_at"] == 1785336668
        assert out["five_hour"]["resets_at"] == 1784821868
        # request URL + headers
        req, timeout = calls[0]
        assert req.full_url == "https://api.kimi.com/coding/v1/usages"
        assert req.headers["Authorization"] == "Bearer tok"
        assert req.headers["User-agent"] == "KimiCLI/1.6"  # Request title-cases
        assert timeout == 2.5

    def test_missing_weekly_omits_seven_day(self, monkeypatch):
        _mock_urlopen(monkeypatch, {"limits": LIVE_PAYLOAD["limits"]})
        out = _fetch_kimi_usage("https://api.kimi.com/coding", "tok")
        assert "seven_day" not in out
        assert out["five_hour"]["pct"] == pytest.approx(16.0)

    def test_missing_5h_window_omits_five_hour(self, monkeypatch):
        _mock_urlopen(monkeypatch, {"usage": LIVE_PAYLOAD["usage"], "limits": []})
        out = _fetch_kimi_usage("https://api.kimi.com/coding", "tok")
        assert "five_hour" not in out
        assert out["seven_day"]["pct"] == pytest.approx(12.0)

    def test_malformed_window_is_omitted_not_fatal(self, monkeypatch):
        payload = {"usage": {"limit": "0", "used": "abc"},
                   "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                               "detail": {"limit": "100", "used": "16",
                                          "resetTime": "not-a-date"}}]}
        _mock_urlopen(monkeypatch, payload)
        assert _fetch_kimi_usage("https://api.kimi.com/coding", "tok") == {}
```

Note on the `resets_at` expectations: they are `int(datetime.fromisoformat(...).timestamp())` of the fixture's `resetTime` values — if your local run disagrees, recompute the fixture's epoch rather than weakening the assertion.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kimi_usage.py::TestFetchKimiUsage -v`
Expected: FAIL with `ImportError: cannot import name '_fetch_kimi_usage'`

- [ ] **Step 3: Implement `_fetch_kimi_usage()`**

In `src/claude_statusbar/core.py`, add imports at the top:

```python
import time
import urllib.request
```

Add right after `_backend()`:

```python
def _parse_kimi_window(detail: dict) -> Optional[dict]:
    """One Kimi quota window ({limit, used, resetTime}, all strings) ->
    {"pct": 0-100, "resets_at": epoch} or None when it doesn't parse."""
    try:
        limit = float(detail.get("limit"))
        used = float(detail.get("used"))
        resets_at = int(datetime.fromisoformat(
            detail["resetTime"].replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    if limit <= 0:
        return None
    return {"pct": used / limit * 100.0, "resets_at": resets_at}


def _fetch_kimi_usage(base_url: str, token: str) -> dict:
    """GET <base_url>/v1/usages and map Kimi's quota onto our two windows:
    weekly (top-level `usage`) -> seven_day, the 300-minute rolling window in
    `limits[]` -> five_hour. Windows that don't parse are omitted, not fatal.
    Raises on transport/HTTP/JSON errors — the caller (_kimi_usage) degrades."""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/usages",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "KimiCLI/1.6"})
    with urllib.request.urlopen(req, timeout=2.5) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    out: dict = {}
    if isinstance(payload, dict):
        weekly = payload.get("usage")
        if isinstance(weekly, dict):
            parsed = _parse_kimi_window(weekly)
            if parsed:
                out["seven_day"] = parsed
        for item in payload.get("limits") or []:
            if not isinstance(item, dict):
                continue
            window = item.get("window") or {}
            try:
                is_5h = (int(window.get("duration") or 0) == 300
                         and window.get("timeUnit") == "TIME_UNIT_MINUTE")
            except (TypeError, ValueError):
                continue
            if is_5h:
                parsed = _parse_kimi_window(item.get("detail") or {})
                if parsed:
                    out["five_hour"] = parsed
                break
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_kimi_usage.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_statusbar/core.py tests/test_kimi_usage.py
git commit -m "feat: fetch and parse Kimi coding-plan 5h/weekly quota"
```

---

### Task 3: `_kimi_usage()` — TTL cache wrapper

**Files:**
- Modify: `src/claude_statusbar/core.py`
- Test: `tests/test_kimi_usage.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_kimi_usage.py`:

```python
import time

from claude_statusbar.core import _kimi_usage

GOOD = {"five_hour": {"pct": 16.0, "resets_at": 1784821868},
        "seven_day": {"pct": 12.0, "resets_at": 1785336668}}


class TestKimiUsageCache:
    def _write_cache(self, path, age_s, limits=GOOD):
        path.write_text(json.dumps(
            {"fetched_at": time.time() - age_s, "limits": limits}))

    def test_fresh_cache_skips_fetch(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        self._write_cache(cache, age_s=10)

        def boom(base, tok):
            raise AssertionError("fetch must not run inside the TTL")

        assert _kimi_usage("b", "t", cache_path=cache, fetcher=boom) == GOOD

    def test_stale_cache_fetches_and_rewrites(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        self._write_cache(cache, age_s=3600, limits={"stale": True})
        new = {"five_hour": {"pct": 1.0, "resets_at": 2}}
        out = _kimi_usage("b", "t", cache_path=cache, fetcher=lambda b, t: new)
        assert out == new
        written = json.loads(cache.read_text())
        assert written["limits"] == new
        assert written["fetched_at"] > time.time() - 60

    def test_fetch_error_serves_stale_cache(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        self._write_cache(cache, age_s=86400)  # any age — errors keep it

        def fail(base, tok):
            raise OSError("timeout")

        assert _kimi_usage("b", "t", cache_path=cache, fetcher=fail) == GOOD

    def test_fetch_error_without_cache_returns_empty(self, tmp_path):
        def fail(base, tok):
            raise OSError("timeout")

        assert _kimi_usage("b", "t", cache_path=tmp_path / "none.json",
                           fetcher=fail) == {}

    def test_missing_token_never_fetches(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        self._write_cache(cache, age_s=86400)

        def boom(base, tok):
            raise AssertionError("fetch must not run without a token")

        assert _kimi_usage("b", "", cache_path=cache, fetcher=boom) == GOOD

    def test_corrupt_cache_is_treated_as_absent(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        cache.write_text("not json{{")

        def fail(base, tok):
            raise OSError("down")

        assert _kimi_usage("b", "t", cache_path=cache, fetcher=fail) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kimi_usage.py::TestKimiUsageCache -v`
Expected: FAIL with `ImportError: cannot import name '_kimi_usage'`

- [ ] **Step 3: Implement `_kimi_usage()`**

In `src/claude_statusbar/core.py`, right after `_fetch_kimi_usage`:

```python
KIMI_USAGE_CACHE = Path.home() / ".cache" / "claude-statusbar" / "kimi_usage.json"
KIMI_USAGE_TTL_S = 60


def _kimi_usage(base_url: str, token: str, *, cache_path=None, fetcher=None) -> dict:
    """Kimi quota with a TTL file cache: at most one HTTP call per
    KIMI_USAGE_TTL_S across the status line's rapid renders. Fresh cache wins;
    on any fetch failure the stale cache is served regardless of age; no cache
    + failure -> {} (limit cells omitted). A missing token never fetches.
    The token is used only in the request header — never cached or logged."""
    path = Path(cache_path) if cache_path else KIMI_USAGE_CACHE
    fetch = fetcher or _fetch_kimi_usage
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cached, dict):
            cached = None
    except (OSError, ValueError):
        cached = None
    if (cached is not None
            and time.time() - cached.get("fetched_at", 0) < KIMI_USAGE_TTL_S):
        return cached.get("limits") or {}
    if not token:
        return (cached or {}).get("limits") or {}
    try:
        limits = fetch(base_url, token)
    except Exception:
        return (cached or {}).get("limits") or {}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"fetched_at": time.time(), "limits": limits}),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return limits
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_kimi_usage.py -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_statusbar/core.py tests/test_kimi_usage.py
git commit -m "feat: TTL-cache Kimi quota fetches, serve stale on error"
```

---

### Task 4: Wire into `parse_stdin_data()` — kimi branch + fallback fix

**Files:**
- Modify: `src/claude_statusbar/core.py` (the rate-limit block, current lines ~122-147)
- Test: `tests/test_kimi_usage.py`

Current code in `parse_stdin_data()`:

```python
        # Rate limits (Claude.ai Pro/Max only)
        rl = data.get('rate_limits', {})
        fh = rl.get('five_hour', {})
        if fh:
            result['rate_limit_pct'] = fh.get('used_percentage', 0)
            result['rate_limit_resets_at'] = fh.get('resets_at')
        sd = rl.get('seven_day', {})
        if sd:
            result['rate_limit_7d_pct'] = sd.get('used_percentage', 0)
            result['rate_limit_7d_resets_at'] = sd.get('resets_at')

        # Fallback: load rate_limits from previous session's cached stdin
        if not fh and not sd:
            try:
                cached = json.loads(debug_file.read_text(encoding="utf-8"))
                ...
            except (OSError, json.JSONDecodeError, TypeError):
                pass
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_kimi_usage.py`:

```python
import io
import sys

from claude_statusbar.core import parse_stdin_data


def _run_parse(monkeypatch, tmp_path, payload, env):
    """parse_stdin_data against a synthetic stdin payload + env. HOME redirects
    the in-function last_stdin.json cache into tmp_path; CTX_STATE_FILE (a
    module-level constant) is patched explicitly so _smooth_context can't
    clobber the real session's state file."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(core, "CTX_STATE_FILE", tmp_path / "ctx_state.json")
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    return parse_stdin_data()


STDIN_RL = {"rate_limits": {
    "five_hour": {"used_percentage": 42.0, "resets_at": 1784821868},
    "seven_day": {"used_percentage": 7.0, "resets_at": 1785336668}}}


class TestParseStdinData:
    def test_default_backend_reads_stdin_rate_limits(self, monkeypatch, tmp_path):
        out = _run_parse(monkeypatch, tmp_path, STDIN_RL, env={})
        assert out["rate_limit_pct"] == 42.0
        assert out["rate_limit_7d_pct"] == 7.0

    def test_kimi_backend_uses_kimi_usage(self, monkeypatch, tmp_path):
        monkeypatch.setattr(core, "_kimi_usage", lambda b, t: GOOD)
        out = _run_parse(monkeypatch, tmp_path, {"model": {}}, env={
            "ANTHROPIC_BASE_URL": "https://api.kimi.com/coding",
            "ANTHROPIC_AUTH_TOKEN": "tok"})
        assert out["rate_limit_pct"] == 16.0
        assert out["rate_limit_resets_at"] == 1784821868
        assert out["rate_limit_7d_pct"] == 12.0
        assert out["rate_limit_7d_resets_at"] == 1785336668

    def test_kimi_backend_skips_stale_anthropic_fallback(self, monkeypatch, tmp_path):
        # A previous default-backend session left Anthropic numbers in the cache…
        cache = tmp_path / ".cache" / "claude-statusbar"
        cache.mkdir(parents=True)
        (cache / "last_stdin.json").write_text(json.dumps(STDIN_RL))
        # …and Kimi has no quota data yet: the bar must show NOTHING, not the
        # stale Anthropic numbers (today's bug).
        monkeypatch.setattr(core, "_kimi_usage", lambda b, t: {})
        out = _run_parse(monkeypatch, tmp_path, {"model": {}}, env={
            "ANTHROPIC_BASE_URL": "https://api.kimi.com/coding",
            "ANTHROPIC_AUTH_TOKEN": "tok"})
        assert "rate_limit_pct" not in out
        assert "rate_limit_7d_pct" not in out

    def test_default_backend_keeps_cache_fallback(self, monkeypatch, tmp_path):
        cache = tmp_path / ".cache" / "claude-statusbar"
        cache.mkdir(parents=True)
        (cache / "last_stdin.json").write_text(json.dumps(STDIN_RL))
        out = _run_parse(monkeypatch, tmp_path, {"model": {}}, env={})
        assert out["rate_limit_pct"] == 42.0
```

- [ ] **Step 2: Run tests to verify the kimi ones fail**

Run: `python -m pytest tests/test_kimi_usage.py::TestParseStdinData -v`
Expected: `test_kimi_backend_skips_stale_anthropic_fallback` FAILS (fallback
populates the keys — the bug being fixed); `test_kimi_backend_uses_kimi_usage`
may fail or error on the unexpected `_kimi_usage` monkeypatch having no effect.
The two default-backend tests should already PASS (regression guard).

- [ ] **Step 3: Restructure the rate-limit block**

In `src/claude_statusbar/core.py`, replace the block quoted above (stdin parse +
cached fallback) with:

```python
        # Rate limits. Which provider's numbers to show depends on the session
        # backend: default (nyx auth) -> Anthropic subscription, from the stdin
        # payload (cached-stdin fallback when a payload omits them); kimi ->
        # Kimi's coding-plan quota from its usages API. The Anthropic fallback
        # must NOT run on a byo backend — it would show a subscription the
        # session isn't consuming (the stale-numbers-on-kimi bug).
        if _backend() == "kimi":
            usage = _kimi_usage(os.environ.get("ANTHROPIC_BASE_URL", ""),
                                os.environ.get("ANTHROPIC_AUTH_TOKEN", ""))
            five = usage.get("five_hour")
            if five:
                result['rate_limit_pct'] = five['pct']
                result['rate_limit_resets_at'] = five['resets_at']
            seven = usage.get("seven_day")
            if seven:
                result['rate_limit_7d_pct'] = seven['pct']
                result['rate_limit_7d_resets_at'] = seven['resets_at']
        else:
            # Anthropic subscription limits (Claude.ai Pro/Max only)
            rl = data.get('rate_limits', {})
            fh = rl.get('five_hour', {})
            if fh:
                result['rate_limit_pct'] = fh.get('used_percentage', 0)
                result['rate_limit_resets_at'] = fh.get('resets_at')
            sd = rl.get('seven_day', {})
            if sd:
                result['rate_limit_7d_pct'] = sd.get('used_percentage', 0)
                result['rate_limit_7d_resets_at'] = sd.get('resets_at')

            # Fallback: load rate_limits from previous session's cached stdin
            if not fh and not sd:
                try:
                    cached = json.loads(debug_file.read_text(encoding="utf-8"))
                    cached_rl = cached.get('rate_limits', {})
                    cached_fh = cached_rl.get('five_hour', {})
                    cached_sd = cached_rl.get('seven_day', {})
                    if cached_fh:
                        result['rate_limit_pct'] = cached_fh.get('used_percentage', 0)
                        result['rate_limit_resets_at'] = cached_fh.get('resets_at')
                    if cached_sd:
                        result['rate_limit_7d_pct'] = cached_sd.get('used_percentage', 0)
                        result['rate_limit_7d_resets_at'] = cached_sd.get('resets_at')
                except (OSError, json.JSONDecodeError, TypeError):
                    pass
```

The `else` branch is the pre-existing code verbatim, only indented — do not
change its logic.

- [ ] **Step 4: Run the full test suite**

Run: `python -m pytest tests/ -v`
Expected: all passed — 21 in `test_kimi_usage.py` plus every pre-existing test
(`test_cli.py`, `test_progress.py`, `test_context_smoothing.py`, `test_updater.py`)

- [ ] **Step 5: Commit**

```bash
git add src/claude_statusbar/core.py tests/test_kimi_usage.py
git commit -m "feat: show kimi-k3 5h/7d quota on kimi backend, stop stale Anthropic fallback"
```

---

### Task 5: Live smoke check against the real endpoint

**Files:** none (manual verification; run from a shell that has the kimi env)

- [ ] **Step 1: Verify the real fetch path end-to-end**

Run (uses the session's own kimi credentials; prints only quota numbers):

```bash
cd /projects/contrib/claude-code-usage-bar/repo && python -c "
import os, sys
sys.path.insert(0, 'src')
from claude_statusbar.core import _kimi_usage
print(_kimi_usage(os.environ['ANTHROPIC_BASE_URL'], os.environ['ANTHROPIC_AUTH_TOKEN']))
"
```

Expected: a dict with `five_hour` and `seven_day` entries, each `{"pct": …, "resets_at": …}` — matching what `curl -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" -H "User-Agent: KimiCLI/1.6" https://api.kimi.com/coding/v1/usages` reports. A cache file now exists at `~/.cache/claude-statusbar/kimi_usage.json`.

- [ ] **Step 2: Verify the status line renders it**

Run the deployed status line in a kimi session (or feed it a synthetic payload
with the kimi env set) and confirm the 5h/7d cells show Kimi's percentages.
Then unset `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` and confirm the bar falls
back to the Anthropic/cached numbers on the default path.

No commit — verification only.
