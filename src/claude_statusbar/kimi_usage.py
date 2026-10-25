"""Kimi coding-plan quota fetching and normalization.

Network access belongs in the detached refresh helper. Render-path callers use
only the cached limits; no bearer token is ever written to the quota cache.
"""
from __future__ import annotations

import json
import math
import urllib.request
from datetime import datetime
from typing import Any, Dict, Optional

from .kimi_cache import _valid_window, is_kimi_url


def _parse_window(detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        limit = float(detail.get("limit"))
        used = float(detail.get("used"))
        resets_at = int(datetime.fromisoformat(
            detail["resetTime"].replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        return None
    if not math.isfinite(limit) or not math.isfinite(used) or limit <= 0:
        return None
    return _valid_window({"pct": used / limit * 100.0,
                          "resets_at": resets_at})


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_usage(base_url: str, token: str) -> dict:
    """Fetch ``/v1/usages`` and map Kimi's 300-minute and weekly windows.

    Invalid individual windows are omitted. Transport, HTTP, and JSON errors
    propagate to the detached refresh worker, which preserves stale cache.
    """
    if not is_kimi_url(base_url):
        raise ValueError("Kimi usage requests require an HTTPS kimi.com URL")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/usages",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "KimiCLI/1.6"})
    opener = urllib.request.build_opener(_NoRedirectHandler())
    with opener.open(req, timeout=2.5) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    out: dict = {}
    if isinstance(payload, dict):
        weekly = payload.get("usage")
        if isinstance(weekly, dict):
            parsed = _parse_window(weekly)
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
                parsed = _parse_window(item.get("detail") or {})
                if parsed:
                    out["five_hour"] = parsed
                break
    return out
