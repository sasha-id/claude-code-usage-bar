"""Tests for context-window debounce (_smooth_context).

Reproduces the real gateway flicker captured from a litellm/OpenRouter session:
the status line receives a stable ~259K context interrupted by a single-refresh
dip to ~165K (a subagent/auxiliary request's usage), then recovers. The meter
must not follow the transient dip.
"""
from claude_statusbar.core import _smooth_context


def _state(tmp_path):
    return str(tmp_path / "ctx_state.json")


def test_transient_dip_is_held(tmp_path):
    sp = _state(tmp_path)
    SID = "sess-A"
    SIZE = 1_000_000
    # stable high reading
    used, _ = _smooth_context(SID, 259178, SIZE, sp)
    assert used == 259178
    # single-refresh dip to a subagent's smaller context -> HELD, not shown
    used, _ = _smooth_context(SID, 164969, SIZE, sp)
    assert used == 259178, "transient dip must be held, not rendered"
    # recovers on the next real reading
    used, _ = _smooth_context(SID, 261271, SIZE, sp)
    assert used == 261271


def test_absent_context_holds_last(tmp_path):
    sp = _state(tmp_path)
    SID = "sess-B"
    _smooth_context(SID, 200000, 1_000_000, sp)
    # a refresh with no context at all (reads as 0) -> hold last stable value
    used, _ = _smooth_context(SID, 0, 0, sp)
    assert used == 200000


def test_growth_passes_through_immediately(tmp_path):
    sp = _state(tmp_path)
    SID = "sess-C"
    _smooth_context(SID, 100000, 1_000_000, sp)
    used, _ = _smooth_context(SID, 108000, 1_000_000, sp)
    assert used == 108000
    used, _ = _smooth_context(SID, 130000, 1_000_000, sp)
    assert used == 130000


def test_confirmed_drop_is_accepted(tmp_path):
    sp = _state(tmp_path)
    SID = "sess-D"
    _smooth_context(SID, 250000, 1_000_000, sp)
    # real /compact: first low reading held...
    used, _ = _smooth_context(SID, 40000, 1_000_000, sp)
    assert used == 250000
    # ...but a second, consistent low reading confirms it -> accepted
    used, _ = _smooth_context(SID, 41000, 1_000_000, sp)
    assert used == 41000


def test_new_session_resets(tmp_path):
    sp = _state(tmp_path)
    _smooth_context("old", 300000, 1_000_000, sp)
    used, _ = _smooth_context("new", 5000, 1_000_000, sp)
    assert used == 5000
