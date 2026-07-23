# Backend-aware 5h/7d limits (default subscription vs kimi-k3)

**Date:** 2026-07-23

**Status:** Implemented

## Problem

The status bar shows 5-hour and 7-day rate-limit bars parsed from the Claude Code
stdin payload (`rate_limits.five_hour` / `rate_limits.seven_day`), which Claude Code
only populates for Anthropic subscription (OAuth) sessions — the nyx launcher's
**Default (nyx auth)** backend.

nyx now ships a second backend, **kimi-k3** (`config/common/nyx.json` in the
sasha.id/nyx repo: `ANTHROPIC_BASE_URL=https://api.kimi.com/coding`,
`ANTHROPIC_AUTH_TOKEN` from 1Password, model `k3[1m]`). On a kimi-k3 session the
stdin payload carries no `rate_limits`, so the bar falls back to the last cached
stdin that had them (`core.py` `parse_stdin_data` fallback block) — displaying
**stale Anthropic subscription numbers for a session that isn't consuming them**.

Kimi's coding plan has the same two windows — a rolling 5-hour request budget and
a weekly quota — exposed via its own API, verified live:

```
GET https://api.kimi.com/coding/v1/usages
Authorization: Bearer <ANTHROPIC_AUTH_TOKEN>   (the same key CC uses for inference)
User-Agent: KimiCLI/1.6

→ {
    "usage":  { "limit": "100", "used": "12", "remaining": "88",
                "resetTime": "2026-07-29T14:51:08.099871Z" },        ← weekly (7d)
    "limits": [ { "window":  { "duration": 300, "timeUnit": "TIME_UNIT_MINUTE" },
                  "detail": { "limit": "100", "used": "16", "remaining": "84",
                              "resetTime": "2026-07-23T15:51:08.099871Z" } } ]   ← 5h
  }
```

## Goal

Detect which backend the session runs on and show that backend's 5h/7d usage:

- **Default (nyx auth):** unchanged — stdin `rate_limits`, with the existing
  cached-stdin fallback.
- **kimi-k3:** fetch Kimi's usages endpoint (throttled), map its 5h and weekly
  windows into the same display slots.

Explicitly out of scope: any launcher or picker changes (the nyx repo is
untouched), switching backends automatically, other byo backends (litellm), and
any new status-bar cells.

## Design

All runtime changes are in `src/claude_statusbar/core.py`, with focused
coverage in the Kimi usage and context-smoothing tests.
`progress.py` is unchanged — the kimi numbers are mapped into the existing
`rate_limit_*` result keys, so they render in the same slots (used% form, same as
Anthropic's percentages).

### Backend detection

`parse_stdin_data()` inspects the process env (inherited from the Claude Code
session, whose statusLine child inherits its environment):

- `ANTHROPIC_BASE_URL` unset/empty → **default**. (The nyx launcher strips
  `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` on the
  non-byo path, so absence is the default-backend signal.)
- A valid `https://kimi.com` or `https://*.kimi.com` URL → **kimi**.
- Malformed, non-HTTPS, lookalike, and all other non-empty URLs → **other**;
  current behavior is retained (cached-stdin fallback). Known wrong-numbers
  limitation there; out of scope.

Detection is a small helper (`_backend()`) returning `"default" | "kimi" | "other"`,
parsed with `urllib.parse.urlparse`; malformed URLs and lookalike hosts are
rejected. An unparseable `ANTHROPIC_BASE_URL` (no host) → `"other"`.

### Kimi usage fetch

New module-level pieces in `core.py`:

- `KIMI_USAGE_CACHE = Path.home() / ".cache" / "claude-statusbar" / "kimi_usage.json"`
- `KIMI_USAGE_TTL_S = 60`
- `_fetch_kimi_usage(base_url, token)` — GET `<base_url>/v1/usages` (base_url
  right-stripped of `/`; with nyx's `https://api.kimi.com/coding` this yields the
  verified `https://api.kimi.com/coding/v1/usages`), stdlib `urllib.request`,
  ~2.5s timeout, `Authorization: Bearer <token>`, `User-Agent: KimiCLI/1.6`.
  Redirects are not followed, so the bearer token cannot be forwarded to a
  different host.
  Returns
  `{"five_hour": {"pct": float, "resets_at": int}, "seven_day": {...}}` with
  `resets_at` as epoch seconds (parsed from the ISO `resetTime`, matching the
  epoch ints the stdin path already produces for `datetime.fromtimestamp`).
  Mapping:
  - 7d ← top-level `usage`: `pct = used/limit*100`, `resets_at` from `resetTime`.
  - 5h ← the `limits[]` entry whose `window` is 300 × `TIME_UNIT_MINUTE` (300
    minutes = 5 hours); same `used/limit` math on its `detail`. If no such entry,
    omit `five_hour`.
  - Values are strings in the payload — parse defensively; a window that doesn't
    parse is omitted, not fatal.
- `_kimi_usage(base_url, token)` — cache wrapper. Fresh cache (< TTL) → return it.
  Stale/absent → acquire an advisory refresh lock, then fetch; on success write
  the cache (atomic temp-file + replace, best-effort) and return. Concurrent
  renders serve the last cache while the lock owner refreshes. On any fetch
  error (timeout, non-200, malformed JSON), return the stale cache regardless
  of age; if there is no cache at all, return `{}` (limit cells omitted for that
  render). The lock is held for the complete read/fetch/write section and is
  released by the OS if the owner exits.
- The token is read from `ANTHROPIC_AUTH_TOKEN` in env and used only in the
  request header — never written to the cache file, logs, or output.

### Integration into `parse_stdin_data()`

After stdin parsing, branch on `_backend()`:

- **default** → exactly today's behavior (stdin `rate_limits` + cached-stdin
  fallback).
- **kimi** → ignore any stdin `rate_limits` (Claude Code shouldn't send them for
  an auth-token backend; if it ever does, the provider API is authoritative for
  this backend) and **skip the cached-stdin fallback** — that fallback is the bug
  that shows stale Anthropic numbers on kimi sessions. Populate the four
  `rate_limit_*` result keys from `_kimi_usage()` instead.
- **other** → today's behavior.

Everything downstream (reset-time formatting, progress-bar rendering) consumes the
same keys and is untouched.

### Error handling

| Condition | Behavior |
|---|---|
| `ANTHROPIC_AUTH_TOKEN` unset on kimi backend | omit cells; cache identity cannot be verified without the token |
| HTTP timeout / non-200 / bad JSON | serve stale cache; omit cells if no cache |
| Weekly `usage` block missing | omit 7d cell only |
| No 300-minute window in `limits[]` | omit 5h cell only |
| Cache file unreadable/corrupt | treat as absent |

The statusline hot path pays at most one ~2.5s-bounded HTTP call per 60s; all
other renders read one small JSON file. A successful fetch also updates a
separate refresh marker, so a cache-payload write failure does not cause a
request on every subsequent render.

### Testing

New `tests/test_kimi_usage.py` (pytest, stdlib-only — repo has zero runtime deps,
keep it that way):

- payload parsing against a fixture matching the verified live response shape
  (weekly + 300-minute window, string values, ISO `resetTime` → epoch)
- missing weekly block / missing 5h window → partial result
- backend detection matrix: unset, `https://api.kimi.com/coding`, unrelated host
- cache: fresh → no HTTP call; stale → fetch and rewrite; fetch error → stale
  served; no cache + fetch error → `{}`
- cache identity: same token on different URLs cannot share a cache entry
- context smoothing: an active owner cannot be reclaimed by a stale-lock check
- regression: default-backend parse output for a stdin payload with `rate_limits`
  is unchanged (same keys, same values)

## Verification

- `pytest tests/test_kimi_usage.py` plus the existing suite (`tests/test_cli.py`,
  `tests/test_progress.py`, `tests/test_context_smoothing.py`) green.
- Manual: run the statusline command with a captured kimi-session stdin payload
  and the kimi env vars set — bar shows Kimi's 5h/7d percentages; unset
  `ANTHROPIC_BASE_URL` and it shows the Anthropic/cached numbers.
