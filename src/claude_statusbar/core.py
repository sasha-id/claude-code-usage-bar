#!/usr/bin/env python3
"""
Claude Code Status Bar Monitor.

Rate-limit, context-window, and model data all arrive via the stdin payload
that Claude Code sends to custom `statusLine` commands. This module parses
that payload and renders the status line — no subprocesses, no file scans on
the hot path.
"""

import json
import re
import sys
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse
import urllib.request

from .progress import format_status_line

# Suppress log output
logging.basicConfig(level=logging.ERROR)

CTX_STATE_FILE = Path.home() / ".cache" / "claude-statusbar" / "ctx_state.json"


def _smooth_context(session_id, cur_used, cur_size, state_path=None):
    """Debounce the context-window reading to stop the 0<->N status-bar flicker.

    When Claude Code is routed through a gateway (litellm/OpenRouter) it hands
    the status line transient context readings on sub-second refreshes: a single
    refresh may report a much smaller context (a subagent / auxiliary request's
    usage) or omit it entirely (reads as 0). Direct Anthropic doesn't do this.

    We keep the last *stable* per-session value and:
      - hold it when a refresh omits context (cur_used <= 0),
      - hold it through a large single-refresh drop (transient dip),
      - accept a large drop only once a second refresh confirms it (real
        /compact), and accept growth / small changes immediately.

    Returns (used_tokens, size) to display.
    """
    path = Path(state_path) if state_path else CTX_STATE_FILE
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        st = {}
    if st.get("session_id") != session_id:
        st = {"session_id": session_id, "shown_used": 0,
              "shown_size": cur_size or 0, "pend_used": None}

    shown_used = st.get("shown_used", 0) or 0
    shown_size = st.get("shown_size", 0) or 0

    if not cur_used or cur_used <= 0:
        # context omitted this refresh -> hold last stable value
        out_used, out_size = shown_used, (shown_size or cur_size)
    elif shown_used and cur_used < shown_used * 0.8:
        # large drop: treat as a transient dip unless a 2nd read confirms it
        pend = st.get("pend_used")
        if pend and abs(cur_used - pend) <= max(2000, int(shown_used * 0.05)):
            st["shown_used"], st["pend_used"] = cur_used, None
            out_used = cur_used
        else:
            st["pend_used"] = cur_used
            out_used = shown_used
        out_size = cur_size or shown_size
    else:
        # growth or small change -> accept immediately
        st["shown_used"], st["pend_used"] = cur_used, None
        out_used, out_size = cur_used, (cur_size or shown_size)

    if cur_size:
        st["shown_size"] = cur_size
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(st), encoding="utf-8")
    except OSError:
        pass
    return out_used, (out_size or cur_size)


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


def _parse_kimi_window(detail: dict) -> dict | None:
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
            if not isinstance(window, dict):
                continue
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


def parse_stdin_data() -> Dict[str, Any]:
    """Parse JSON data injected by Claude Code via stdin.

    Claude Code sends rich session data including model, cost, context window,
    and (for Pro/Max) rate limits.  We extract everything useful so the
    statusbar can display official numbers without spawning subprocesses.
    """
    result: Dict[str, Any] = {}
    try:
        if sys.stdin.isatty():
            return result
        raw = sys.stdin.read()
        if not raw:
            return result

        debug_file = Path.home() / ".cache" / "claude-statusbar" / "last_stdin.json"
        data = json.loads(raw)

        # Only cache stdin when it contains rate_limits (avoid overwriting with empty data)
        if data.get('rate_limits', {}).get('five_hour'):
            try:
                debug_file.parent.mkdir(parents=True, exist_ok=True)
                debug_file.write_text(raw, encoding="utf-8")
            except OSError:
                pass

        # Session ID
        result['session_id'] = data.get('session_id', '')

        # Working directory (used by git_info for branch/worktree detection)
        result['cwd'] = data.get('cwd', '')

        # Model
        model_obj = data.get('model', {})
        if isinstance(model_obj, dict):
            result['model_id'] = model_obj.get('id', '')
            result['display_name'] = model_obj.get('display_name', '')

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

        # Context window
        cw = data.get('context_window', {})
        if cw:
            result['context_used_pct'] = cw.get('used_percentage', 0)
            result['context_remaining_pct'] = cw.get('remaining_percentage', 100)
            result['context_window_size'] = cw.get('context_window_size', 0)
            result['total_input_tokens'] = cw.get('total_input_tokens', 0)
            result['total_output_tokens'] = cw.get('total_output_tokens', 0)

        # Debounce context to stop gateway-induced 0<->N flicker (litellm/OpenRouter).
        # No-op for direct Anthropic, whose readings grow monotonically.
        size = result.get('context_window_size', 0)
        cur_used = result.get('total_input_tokens', 0) or 0
        if not cur_used:
            pct = result.get('context_used_pct', 0)
            if pct and size:
                cur_used = int(size * pct / 100)
        sm_used, sm_size = _smooth_context(result.get('session_id', ''), cur_used, size)
        if sm_size:
            result['context_window_size'] = sm_size
            result['context_used_pct'] = round(100.0 * sm_used / sm_size)
        result['total_input_tokens'] = sm_used

        # Session cost
        cost = data.get('cost', {})
        if cost:
            result['session_cost_usd'] = cost.get('total_cost_usd', 0.0)
            result['total_duration_ms'] = cost.get('total_duration_ms', 0)
            result['lines_added'] = cost.get('total_lines_added', 0)
            result['lines_removed'] = cost.get('total_lines_removed', 0)

        # Version
        result['claude_version'] = data.get('version', '')

        # Reasoning effort / thinking / fast-mode (Claude Code >= v2.1.119).
        # These are the LIVE session values — effort.level updates on mid-session
        # /effort changes. `effort` is absent for models without effort support.
        effort = data.get('effort', {})
        if isinstance(effort, dict) and effort.get('level'):
            result['effort_level'] = effort.get('level')
        thinking = data.get('thinking', {})
        if isinstance(thinking, dict):
            result['thinking_enabled'] = bool(thinking.get('enabled'))
        result['fast_mode'] = bool(data.get('fast_mode'))

        # Mark that we have valid stdin data
        result['_has_stdin'] = True

    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return result


def is_bypass_permissions_active() -> bool:
    """Detect whether bypass-permissions mode is currently active.

    Claude Code does not expose this via the statusline stdin payload, so we
    use a best-effort multi-source approach:
      1. CLAUDE_SKIP_PERMISSIONS env var (set by some wrappers)
      2. settings.json defaultMode == 'bypassPermissions'
    """
    # 1. Explicit env var
    env_val = os.environ.get('CLAUDE_SKIP_PERMISSIONS', '').lower()
    if env_val in ('1', 'true', 'yes'):
        return True

    # 2. settings.json defaultMode
    try:
        settings_path = Path.home() / '.claude' / 'settings.json'
        if settings_path.exists():
            with open(settings_path, 'r', encoding='utf-8') as f:
                settings = json.load(f)
            if settings.get('defaultMode') == 'bypassPermissions':
                return True
    except Exception:
        pass

    return False


def get_current_model(stdin_data: Optional[Dict[str, Any]] = None) -> tuple[str, str]:
    """Return (model_id, display_name), using stdin data when available."""
    sd = stdin_data or {}
    model = sd.get('model_id') or 'unknown'
    display_name = sd.get('display_name') or ''
    if not display_name:
        display_name = model if model != 'unknown' else 'Unknown'
    return model, display_name


def calculate_reset_time(reset_hour: Optional[int] = None) -> str:
    """Estimate time until the 5-hour window resets.

    Only used in the error-path fallback; the happy path derives reset times
    directly from the official `resets_at` timestamps in the stdin payload.
    """
    # If the user pins a reset hour, honor it.
    if reset_hour is not None and 0 <= reset_hour <= 23:
        now = datetime.now()
        target = now.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        diff = target - now
        total_minutes = int(diff.total_seconds() / 60)
        hours = total_minutes // 60
        mins = total_minutes % 60
        return f"{hours}h {mins:02d}m"

    # Fallback: assume a fixed 2 PM reset (matches the original project default).
    now = datetime.now()
    today_2pm = now.replace(hour=14, minute=0, second=0, microsecond=0)
    tomorrow_2pm = today_2pm + timedelta(days=1)
    next_reset = tomorrow_2pm if now >= today_2pm else today_2pm
    diff = next_reset - now

    total_minutes = int(diff.total_seconds() / 60)
    hours = total_minutes // 60
    mins = total_minutes % 60
    return f"{hours}h {mins:02d}m"


def check_for_updates(session_id: str = ''):
    """Kick off a background update check once per new session.

    MUST return quickly — this runs synchronously in the statusline's hot
    path. The actual HTTP fetch and `uv tool install --upgrade` happen in
    a detached subprocess, so Claude Code's startup never waits on PyPI.
    Any upgrade takes effect silently at the next statusline invocation.

    Disabled by setting env CLAUDE_STATUSBAR_NO_UPDATE=1 or
    passing --no-auto-update on the CLI.
    """
    # Respect opt-out
    env_val = os.environ.get('CLAUDE_STATUSBAR_NO_UPDATE', '').lower()
    if env_val in ('1', 'true', 'yes'):
        return

    try:
        cache_dir = Path.home() / '.cache' / 'claude-statusbar'
        cache_dir.mkdir(parents=True, exist_ok=True)
        last_session_file = cache_dir / 'last_update_session'

        # Skip if we've already kicked off a check for this session.
        if session_id and last_session_file.exists():
            try:
                if last_session_file.read_text().strip() == session_id:
                    return
            except OSError:
                pass

        # Claim the session marker BEFORE spawning so concurrent statusline
        # invocations during the same startup burst don't fire duplicate
        # background checks (which would race on `uv tool install`).
        if session_id:
            try:
                last_session_file.write_text(session_id)
            except OSError:
                pass

        from .updater import spawn_update_check_background
        spawn_update_check_background()

    except Exception:
        # Never let an update-check error bubble up into the statusline.
        pass


def format_number(num: float) -> str:
    """Format number for detail display."""
    if num >= 1_000_000:
        return f"{num/1_000_000:.1f}M"
    elif num >= 1_000:
        return f"{num/1_000:.1f}k"
    return f"{num:.0f}"


def main(json_output: bool = False,
         reset_hour: Optional[int] = None, use_color: bool = True,
         detail: bool = False, pet_name: Optional[str] = None,
         show_pet: bool = True,
         warning_threshold: float = 30.0, critical_threshold: float = 70.0,
         show_git: bool = True):
    """Main function"""
    from .pet import format_pet
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

    try:
        if not json_output:
            check_for_updates(stdin_data.get('session_id', ''))

        has_official = (stdin_data.get('rate_limit_pct') is not None or
                        stdin_data.get('rate_limit_7d_pct') is not None)

        model_id, display_name = get_current_model(stdin_data)
        bypass = is_bypass_permissions_active()

        if has_official:
            # ✅ Official data from Anthropic API headers (Claude Code ≥ v2.1.80)
            msgs_pct = stdin_data.get('rate_limit_pct')
            weekly_pct = stdin_data.get('rate_limit_7d_pct')

            resets_at = stdin_data.get('rate_limit_resets_at')
            if resets_at:
                diff = datetime.fromtimestamp(resets_at, tz=timezone.utc) - datetime.now(timezone.utc)
                total_min = max(0, int(diff.total_seconds() / 60))
                minutes_to_reset = total_min
                hours, mins = total_min // 60, total_min % 60
                if hours > 0:
                    reset_time = f"{hours}h{mins:02d}m"
                else:
                    reset_time = f"{mins}m"
            else:
                reset_time = "--"
                minutes_to_reset = None

            resets_at_7d = stdin_data.get('rate_limit_7d_resets_at')
            if resets_at_7d:
                diff_7d = datetime.fromtimestamp(resets_at_7d, tz=timezone.utc) - datetime.now(timezone.utc)
                total_sec_7d = max(0, int(diff_7d.total_seconds()))
                days_7d = total_sec_7d // 86400
                hours_7d = (total_sec_7d % 86400) // 3600
                mins_7d = (total_sec_7d % 3600) // 60
                if days_7d > 0:
                    reset_time_7d = f"{days_7d}d{hours_7d:02d}h"
                elif hours_7d > 0:
                    reset_time_7d = f"{hours_7d}h{mins_7d:02d}m"
                else:
                    reset_time_7d = f"{mins_7d}m"
            else:
                reset_time_7d = ""

            model = display_name if display_name != 'Unknown' else model_id

            if json_output:
                print(json.dumps({
                    "success": True, "source": "official",
                    "rate_limits": {
                        "five_hour": {"used_percentage": msgs_pct, "reset_time": reset_time},
                        "seven_day": {"used_percentage": weekly_pct, "reset_time": reset_time_7d},
                    },
                    "meta": {"model": model_id, "display_name": display_name,
                             "reset_time": reset_time, "reset_time_7d": reset_time_7d, "bypass": bypass,
                             "effort": stdin_data.get('effort_level'),
                             "thinking": stdin_data.get('thinking_enabled', False),
                             "fast_mode": stdin_data.get('fast_mode', False),
                             },
                }))
            else:
                # Append context window usage to model name: Opus 4.6(10k/1M)
                ctx_size = stdin_data.get('context_window_size', 0)
                ctx_pct = stdin_data.get('context_used_pct', 0)
                if ctx_pct and ctx_size:
                    ctx_used = int(ctx_size * ctx_pct / 100)
                else:
                    ctx_used = stdin_data.get('total_input_tokens', 0) + stdin_data.get('total_output_tokens', 0)
                if ctx_size > 0:
                    # Strip redundant size suffix like "(1M context)" from display_name
                    model = re.sub(r'\s*\([^)]*context[^)]*\)', '', model)
                    model = f"{model}({format_number(ctx_used)}/{format_number(ctx_size)})"

                session_id = stdin_data.get('session_id', '')
                current_hour = datetime.now().hour
                pet_text = ""
                if show_pet:
                    pet_pct = msgs_pct if msgs_pct is not None else 0
                    pet_text = format_pet(
                        pet_pct, current_hour, session_id, minutes_to_reset, pet_name
                    )

                print(format_status_line(
                    msgs_pct=msgs_pct, tkns_pct=None,
                    reset_time=reset_time, model=model,
                    weekly_pct=weekly_pct,
                    reset_time_7d=reset_time_7d,
                    bypass=bypass, use_color=use_color,
                    pet_text=pet_text,
                    warning_threshold=warning_threshold,
                    critical_threshold=critical_threshold,
                    effort_level=stdin_data.get('effort_level'),
                    thinking_enabled=stdin_data.get('thinking_enabled', False),
                    fast_mode=stdin_data.get('fast_mode', False),
                    branch=git_branch, worktree=git_worktree,
                ))
        else:
            # No rate_limits yet — could be session start or old Claude Code
            model = display_name if display_name != 'Unknown' else model_id
            version = stdin_data.get('claude_version', '') if stdin_data.get('_has_stdin') else ''

            if stdin_data.get('_has_stdin'):
                # Have stdin but no rate_limits — session just started, show placeholders
                ctx_size = stdin_data.get('context_window_size', 0)
                ctx_pct = stdin_data.get('context_used_pct', 0)
                if ctx_pct and ctx_size:
                    ctx_used = int(ctx_size * ctx_pct / 100)
                else:
                    ctx_used = stdin_data.get('total_input_tokens', 0) + stdin_data.get('total_output_tokens', 0)
                if ctx_size > 0:
                    model = re.sub(r'\s*\([^)]*context[^)]*\)', '', model)
                    model = f"{model}({format_number(ctx_used)}/{format_number(ctx_size)})"

                if json_output:
                    print(json.dumps({
                        "success": True, "source": "waiting",
                        "meta": {"model": model_id, "display_name": display_name,
                                 "claude_version": version, "bypass": bypass},
                    }))
                else:
                    session_id = stdin_data.get('session_id', '')
                    current_hour = datetime.now().hour
                    pet_text = ""
                    if show_pet:
                        pet_text = format_pet(0, current_hour, session_id, None, pet_name)
                    print(format_status_line(
                        msgs_pct=None, tkns_pct=None,
                        reset_time="--", model=model,
                        weekly_pct=None,
                        bypass=bypass, use_color=use_color,
                        pet_text=pet_text,
                        warning_threshold=warning_threshold,
                        critical_threshold=critical_threshold,
                        effort_level=stdin_data.get('effort_level'),
                        thinking_enabled=stdin_data.get('thinking_enabled', False),
                        fast_mode=stdin_data.get('fast_mode', False),
                        branch=git_branch, worktree=git_worktree,
                    ))
            else:
                # No stdin at all — not running inside Claude Code statusLine
                if json_output:
                    print(json.dumps({
                        "success": False,
                        "error": "No stdin data. Run inside Claude Code statusLine.",
                        "meta": {"model": model_id, "display_name": display_name,
                                 "bypass": bypass},
                    }))
                else:
                    print(f"⚠ Run inside Claude Code statusLine for rate-limit data | {model}")

    except Exception as e:
        reset_time = calculate_reset_time(reset_hour=reset_hour).replace(" ", "")
        model_id, display_name = get_current_model(stdin_data)
        bypass = is_bypass_permissions_active()
        if json_output:
            print(json.dumps({"success": False, "error": str(e)}))
        else:
            current_hour = datetime.now().hour
            pet_text = ""
            if show_pet:
                pet_text = format_pet(0, current_hour, '', None, pet_name)
            print(format_status_line(
                msgs_pct=None, tkns_pct=None,
                reset_time=reset_time, model=display_name,
                weekly_pct=None,
                bypass=bypass, use_color=use_color,
                pet_text=pet_text,
                warning_threshold=warning_threshold,
                critical_threshold=critical_threshold,
            ))


if __name__ == '__main__':
    main()
