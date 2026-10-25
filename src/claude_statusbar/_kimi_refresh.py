"""Detached Kimi quota refresh; spawned by the live-credential client.

The bearer token arrives only through ``CS_KIMI_TOKEN`` in the child
environment, never argv or disk. Every failure preserves stale quota data and
records a short retry backoff so the statusline cannot spawn a request storm.
"""
from __future__ import annotations

import os
import sys
import time

from . import git_cache, kimi_cache, kimi_usage


def refresh(base: str, token: str, fp: str = "") -> int:
    fp = fp or kimi_cache.fingerprint(base, token)
    lock = git_cache.try_claim("kimi:" + fp)
    if lock is None:
        return 0
    try:
        if kimi_cache.is_fresh(kimi_cache.read_cache(fp)):
            return 0
        try:
            limits = kimi_usage.fetch_usage(base, token)
            if not limits:
                raise ValueError("no valid Kimi quota windows")
            entry = {"ts": time.time(), "limits": limits}
        except Exception:
            previous = kimi_cache.read_cache(fp)
            now = time.time()
            entry = dict(previous) if previous else {"ts": now, "limits": {}}
            entry.update(attempted_at=now, refresh_failed=True)
        if kimi_cache.write_cache_atomic(fp, entry):
            kimi_cache.clear_inflight(fp)
        return 0
    finally:
        lock.close()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0
    return refresh(argv[1], os.environ.get("CS_KIMI_TOKEN", ""),
                   os.environ.get("CS_KIMI_FP", ""))


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        sys.exit(0)
