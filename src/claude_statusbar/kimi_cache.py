"""Secret-free, account-scoped cache for Kimi coding-plan quotas.

This module stays lightweight for the render hot path. Network access lives in
``kimi_usage`` and is only imported by the detached refresh helper.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .cache import atomic_write_text


TTL_SECONDS = 300
FAILED_RETRY_SECONDS = 30
INFLIGHT_MAX_AGE_S = 60


def _cache_root() -> Path:
    return Path(os.path.expanduser("~")) / ".cache" / "claude-statusbar" / "kimi"


def is_kimi_url(base_url: str) -> bool:
    """True only for HTTPS Kimi hosts, never look-alike domains."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse((base_url or "").strip())
        host = (parsed.hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    return (parsed.scheme.lower() == "https"
            and (host == "kimi.com" or host.endswith(".kimi.com")))


def fingerprint(base_url: str, token: str) -> str:
    """Stable per-(URL, token) bucket id; the raw token never touches disk."""
    digest = hashlib.sha1()
    digest.update((base_url or "").encode("utf-8"))
    digest.update(b"\x00")
    digest.update((token or "").encode("utf-8"))
    return digest.hexdigest()


def cache_path_for(fp: str) -> Path:
    return _cache_root() / f"{fp}.json"


def _valid_window(window: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(window, dict):
        return None
    pct = window.get("pct")
    resets_at = window.get("resets_at")
    if (isinstance(pct, bool) or not isinstance(pct, (int, float))
            or isinstance(resets_at, bool)
            or not isinstance(resets_at, (int, float))):
        return None
    try:
        pct_value = float(pct)
        reset_value = float(resets_at)
        reset_epoch = int(resets_at)
    except (OverflowError, ValueError):
        return None
    if (not math.isfinite(pct_value) or not math.isfinite(reset_value)
            or pct_value < 0 or reset_value <= 0 or reset_epoch != resets_at):
        return None
    try:
        datetime.fromtimestamp(reset_epoch, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return {"pct": pct_value, "resets_at": reset_epoch}


def _valid_limits(limits: Any) -> Optional[Dict[str, Dict[str, Any]]]:
    if not isinstance(limits, dict):
        return None
    out = {}
    for name in ("five_hour", "seven_day"):
        if name in limits:
            window = _valid_window(limits[name])
            if window is not None:
                out[name] = window
    return out or None


def read_cache(fp: str) -> Optional[dict]:
    try:
        entry = json.loads(cache_path_for(fp).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(entry, dict):
        return None
    limits = _valid_limits(entry.get("limits"))
    if limits is None and entry.get("refresh_failed") and entry.get("limits") == {}:
        limits = {}
    ts = entry.get("ts")
    if limits is None or isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        if not math.isfinite(ts):
            return None
    except OverflowError:
        return None
    return {**entry, "limits": limits}


def cached_limits(env: Dict[str, str]) -> Optional[Dict[str, Dict[str, Any]]]:
    """Read cached limits only; never perform or schedule network work."""
    fp = env.get("CS_KIMI_FP", "")
    if not fp:
        base = env.get("ANTHROPIC_BASE_URL", "").strip()
        token = (env.get("ANTHROPIC_API_KEY", "").strip()
                 or env.get("ANTHROPIC_AUTH_TOKEN", "").strip())
        if not base or not token:
            return None
        fp = fingerprint(base, token)
    entry = read_cache(fp)
    return (entry["limits"] or None) if entry else None


def is_fresh(entry: Optional[dict], now: Optional[float] = None) -> bool:
    if not entry:
        return False
    ts = entry.get("attempted_at" if entry.get("refresh_failed") else "ts",
                   entry.get("ts"))
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    try:
        if not math.isfinite(ts):
            return False
    except OverflowError:
        return False
    ttl = FAILED_RETRY_SECONDS if entry.get("refresh_failed") else TTL_SECONDS
    age = (time.time() if now is None else now) - ts
    return 0 <= age < ttl


def write_cache_atomic(fp: str, entry: dict) -> bool:
    return atomic_write_text(cache_path_for(fp), json.dumps(entry), durable=False)


def _inflight_path(fp: str) -> Path:
    return cache_path_for(fp).with_suffix(".inflight")


def is_inflight(fp: str) -> bool:
    try:
        data = json.loads(_inflight_path(fp).read_text(encoding="utf-8"))
        ts = data.get("ts", 0)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(ts, (int, float)) and time.time() - ts < INFLIGHT_MAX_AGE_S


def mark_inflight(fp: str) -> None:
    path = _inflight_path(fp)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}),
                    encoding="utf-8")


def clear_inflight(fp: str) -> None:
    try:
        _inflight_path(fp).unlink()
    except FileNotFoundError:
        pass
