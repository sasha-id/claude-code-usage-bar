"""Gateway context-window debounce on upstream's authoritative usage values.

A litellm/OpenRouter session can alternate a stable main-request reading with a
single smaller auxiliary-request reading. Large drops need two consistent
readings; growth and small changes pass through immediately.
"""
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

from claude_statusbar import core


@pytest.fixture(autouse=True)
def _state_path(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CTX_STATE_FILE", tmp_path / "ctx_state.json",
                        raising=False)


def _usage(session_id, used, size, *, current_usage=False):
    data = {"session_id": session_id, "context_window_size": size}
    if used is not None:
        pct = used / size * 100 if size else 0
        data.update(context_used_pct=pct, total_input_tokens=used)
        if current_usage:
            data["context_current_usage"] = {
                "input_tokens": used,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }
    else:
        data["context_used_pct"] = None
    return core._context_window_usage(data, env={})


def test_transient_dip_is_held():
    assert _usage("sess-A", 259178, 1_000_000)[2] == 259178
    assert _usage("sess-A", 164969, 1_000_000)[2] == 259178
    assert _usage("sess-A", 261271, 1_000_000)[2] == 261271


def test_absent_context_holds_last():
    _usage("sess-B", 200000, 1_000_000)
    pct, size, used = core._context_window_usage(
        {"session_id": "sess-B"}, env={})
    assert used == 200000
    assert size == 1_000_000
    assert pct == pytest.approx(20.0)


def test_absent_usage_holds_prior_context_size():
    _usage("sess-B-size", 250000, 1_000_000)

    _, size, used = _usage("sess-B-size", None, 100000)
    assert used == 250000
    assert size == 1_000_000

    _, size, _ = core._context_window_usage(
        {"session_id": "sess-B-size"}, env={})
    assert size == 1_000_000


def test_growth_passes_through_immediately():
    _usage("sess-C", 100000, 1_000_000)
    assert _usage("sess-C", 108000, 1_000_000)[2] == 108000
    assert _usage("sess-C", 130000, 1_000_000)[2] == 130000


def test_confirmed_drop_is_accepted_with_new_size():
    _usage("sess-D", 250000, 1_000_000)
    assert _usage("sess-D", 40000, 1_000_000)[2] == 250000

    pct, size, used = _usage("sess-D", 41000, 100000)
    assert used == 41000
    assert size == 100000
    assert pct == pytest.approx(41.0)


def test_transient_dip_holds_prior_context_size():
    _usage("sess-size", 250000, 1_000_000)
    _, size, used = _usage("sess-size", 40000, 100000)
    assert used == 250000
    assert size == 1_000_000


def test_new_session_resets():
    _usage("old", 300000, 1_000_000)
    assert _usage("new", 5000, 1_000_000)[2] == 5000


def test_interleaved_sessions_keep_independent_history():
    _usage("session-a", 300000, 1_000_000)
    _usage("session-b", 200000, 1_000_000)
    assert _usage("session-a", 5000, 1_000_000)[2] == 300000


def test_missing_session_id_does_not_reuse_persisted_history():
    _usage("session-a", 300000, 1_000_000)
    assert _usage("", 5000, 100000)[1:] == (100000, 5000)


def test_authoritative_current_usage_is_debounced():
    _usage("session-current", 250000, 1_000_000, current_usage=True)
    _, _, used = _usage("session-current", 40000, 1_000_000,
                        current_usage=True)
    assert used == 250000


def test_unknown_first_tick_does_not_fabricate_zero():
    assert _usage("session-unknown", None, 1_000_000)[2] is None


def test_explicit_null_current_usage_stays_unknown_after_compact():
    _usage("session-compact", 250000, 1_000_000)
    pct, size, used = core._context_window_usage({
        "session_id": "session-compact",
        "context_window_size": 1_000_000,
        "context_used_pct": None,
        "context_current_usage": None,
        "total_input_tokens": 73_623_000,
    }, env={})
    assert pct is None
    assert size == 1_000_000
    assert used is None


@pytest.mark.parametrize("null_size", [1_000_000, 0])
def test_explicit_null_resets_history_before_later_ticks(null_size):
    session = f"session-compact-reset-{null_size}"
    _usage(session, 250000, 1_000_000)

    _, _, used = core._context_window_usage({
        "session_id": session,
        "context_window_size": null_size,
        "context_used_pct": None,
        "context_current_usage": None,
        "total_input_tokens": 73_623_000,
    }, env={})
    assert used in (None, 0)

    _, _, used = core._context_window_usage({"session_id": session}, env={})
    assert used in (None, 0)
    assert _usage(session, 40000, 1_000_000)[2] == 40000


def test_real_zero_is_confirmed_not_treated_as_absent():
    _usage("session-zero", 250000, 1_000_000, current_usage=True)
    assert _usage("session-zero", 0, 1_000_000,
                  current_usage=True)[2] == 250000
    assert _usage("session-zero", 0, 1_000_000,
                  current_usage=True)[2] == 0


def test_parse_preserves_official_percentage_and_current_usage(monkeypatch):
    current = {"input_tokens": 100000,
               "cache_creation_input_tokens": 0,
               "cache_read_input_tokens": 0}
    payload = {
        "session_id": "session-context",
        "context_window": {
            "used_percentage": 75,
            "context_window_size": 200000,
            "total_input_tokens": 100000,
            "total_output_tokens": 50000,
            "current_usage": current,
        },
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    result = core.parse_stdin_data()
    pct, _, used = core._context_window_usage(result, env={})

    assert pct == 75
    assert used == 100000
    assert result["context_current_usage"] == current


def test_context_state_files_are_owner_only_on_posix(tmp_path):
    if os.name == "nt":
        return
    _usage("session-permissions", 100000, 1_000_000)

    path = core.CTX_STATE_FILE
    assert path.stat().st_mode & 0o777 == 0o600
    assert Path(f"{path}.lock").stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_overlapping_update_keeps_last_complete_state():
    from claude_statusbar import context_state
    _usage("sess-lock", 200000, 1_000_000)
    lease = context_state.claim_file_lease(core.CTX_STATE_FILE)
    try:
        _, size, used = _usage("sess-lock", 1000, 100000)
    finally:
        context_state.release_file_lease(lease)

    assert used == 200000
    assert size == 1_000_000


def test_live_owner_cannot_be_reclaimed_as_stale(tmp_path):
    from claude_statusbar import context_state
    lease = context_state.claim_file_lease(core.CTX_STATE_FILE)
    assert lease is not None
    try:
        lock_path = Path(f"{core.CTX_STATE_FILE}.lock")
        old = time.time() - 3600
        os.utime(lock_path, (old, old))
        assert context_state.claim_file_lease(core.CTX_STATE_FILE) is None
    finally:
        context_state.release_file_lease(lease)
