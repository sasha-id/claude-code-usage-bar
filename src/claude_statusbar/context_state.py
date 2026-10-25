"""Nonblocking per-session context-window debounce state."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .cache import atomic_write_text


MAX_SESSIONS = 32


def claim_file_lease(path: Path):
    """Try to hold an advisory lock for the whole read/modify/write section."""
    lease_path = path.with_name(f"{path.name}.lock")
    try:
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(lease_path.parent, 0o700)
        except OSError:
            pass
        fd = os.open(str(lease_path), os.O_RDWR | os.O_CREAT, 0o600)
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        try:
            os.close(fd)
        except (NameError, OSError):
            pass
        return None
    return fd


def release_file_lease(lease) -> None:
    if lease is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(lease, 0, os.SEEK_SET)
            msvcrt.locking(lease, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lease, fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass
    finally:
        try:
            os.close(lease)
        except OSError:
            pass


def _read_sessions(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    sessions = raw.get("sessions")
    if isinstance(sessions, dict):
        return sessions
    # Migrate the original single-session on-disk shape if encountered.
    if "session_id" in raw:
        return {str(raw.get("session_id") or ""): {
            "shown_used": raw.get("shown_used"),
            "shown_size": raw.get("shown_size", 0) or 0,
            "pend_used": raw.get("pend_used"),
        }}
    return {}


def _valid_used(value):
    return value if type(value) is int and value >= 0 else None


def _valid_size(value):
    return value if type(value) is int and value > 0 else 0


def reset(session_id, path: Path) -> None:
    """Forget a session after an explicit post-compact unknown sentinel."""
    session_key = str(session_id or "")
    if not session_key:
        return
    lease = claim_file_lease(path)
    if lease is None:
        return
    try:
        sessions = _read_sessions(path)
        if sessions.pop(session_key, None) is not None:
            atomic_write_text(path, json.dumps({"sessions": sessions}), durable=False)
    finally:
        release_file_lease(lease)


def smooth(session_id, cur_used, cur_size, path: Path):
    """Return the debounced ``(used, size)`` without ever waiting for a lock."""
    session_key = str(session_id or "")
    if not session_key:
        return cur_used, cur_size
    lease = claim_file_lease(path)
    if lease is None:
        state = _read_sessions(path).get(session_key)
        if not isinstance(state, dict):
            return cur_used, cur_size
        shown_used = _valid_used(state.get("shown_used"))
        if shown_used is None:
            return cur_used, cur_size
        return shown_used, _valid_size(state.get("shown_size")) or cur_size

    try:
        sessions = _read_sessions(path)
        state = sessions.pop(session_key, None)
        if not isinstance(state, dict):
            state = {"shown_used": None, "shown_size": cur_size or 0,
                     "pend_used": None}
        shown_used = _valid_used(state.get("shown_used"))
        shown_size = _valid_size(state.get("shown_size"))
        hold_size = False

        if cur_used is None:
            out_used, out_size = shown_used, shown_size or cur_size
            hold_size = True
        elif shown_used is not None and cur_used < shown_used * 0.8:
            pending = _valid_used(state.get("pend_used"))
            if (pending is not None and abs(cur_used - pending)
                    <= max(2000, int(shown_used * 0.05))):
                state["shown_used"], state["pend_used"] = cur_used, None
                out_used, out_size = cur_used, cur_size or shown_size
            else:
                state["pend_used"] = cur_used
                out_used, out_size = shown_used, shown_size or cur_size
                hold_size = True
        else:
            state["shown_used"], state["pend_used"] = cur_used, None
            out_used, out_size = cur_used, cur_size or shown_size

        if cur_size and not hold_size:
            state["shown_size"] = cur_size
        state["updated_at"] = time.time()
        sessions[session_key] = state
        while len(sessions) > MAX_SESSIONS:
            sessions.pop(next(iter(sessions)))
        atomic_write_text(path, json.dumps({"sessions": sessions}), durable=False)
        return out_used, out_size or cur_size
    finally:
        release_file_lease(lease)
