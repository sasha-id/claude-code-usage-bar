"""Compact single-row Aurora layout."""
from types import SimpleNamespace

from claude_statusbar import styles
from claude_statusbar.identity import IdentityInfo
from claude_statusbar.themes import get_theme


def _identity(**changes):
    values = dict(
        project_name="nyx", in_git=True, branch="chore/api-smoke-refresh",
        detached=False, worktree_name="api-smoke-refresh", toplevel="/x",
        is_worktree=True, worktree_count=12,
    )
    values.update(changes)
    return IdentityInfo(**values)


def _render(**changes):
    kwargs = dict(
        msgs_pct=17, weekly_pct=7, reset_5h="3h03m", reset_7d="4d09h",
        model="k3.1-pro[1m](75.1k/1.0M)", ctx_pct=8,
        effort_level="high", thinking_enabled=True, fast_mode=False,
        cache_age_text="4m36s", countdown_emoji="⚡",
        projection_5h="→36%", projection_7d="→61%", forecast_5h="~8m",
        show_project_branch=True, identity=_identity(), identity_dirty=False,
        identity_duration="4m39s", identity_lines="+20 -3",
        use_color=False, theme=get_theme("graphite"),
    )
    kwargs.update(changes)
    return styles.render("aurora", **kwargs)


def test_compact_single_row_layout():
    assert _render() == (
        "5h ■■■■■■■■ 17% 3h03m · 7d ■■■■■■■■  7% 4d09h"
        " │ k3.1-pro[1m] ✳high 75.1k/1.0M"
        " │ ⎇ chore/api-smoke-refresh ⑂api-smoke-refresh · ❄ 4m39s"
    )


def test_stacked_layout_keeps_compact_quota_separator():
    out = _render(max_width=60, narrow_layout="stack")

    assert "3h03m · 7d" in out
    assert "3h03m │ 7d" not in out


def test_main_checkout_omits_worktree_and_repo():
    out = _render(identity=_identity(
        branch="main", is_worktree=False, worktree_name=None,
        worktree_count=12,
    ))

    assert out.endswith("│ ⎇ main · ❄ 4m39s")
    assert "⑂" not in out
    assert "🌳" not in out
    assert "nyx" not in out


def test_identity_and_duration_are_optional():
    out = _render(show_project_branch=False, identity=None,
                  identity_duration="")

    assert "\n" not in out
    assert "⎇" not in out
    assert "❄" not in out


def test_color_layout_keeps_inline_worktree():
    info = SimpleNamespace(
        project_name="nyx", in_git=True, branch="topic", detached=False,
        worktree_name="wt-x", toplevel="/x", is_worktree=True,
        worktree_count=2, web_url=None,
    )
    out = _render(identity=info, use_color=True)

    assert "⑂wt-x" in out
    assert "\n" not in out
    assert "\x1b[" in out


def test_core_uses_precise_session_duration_and_skips_cache_scan(
        tmp_path, monkeypatch, capsys):
    import io
    import json
    import sys

    from claude_statusbar import config, core, identity

    path = tmp_path / ".claude" / "claude-statusbar.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "style": "aurora", "show_duration": True, "show_cache_age": True,
        "show_project_branch": True, "show_fp_risk": False,
        "show_per_model": False, "show_todos": False,
        "show_projection": False, "show_forecast": False,
    }))
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("CS_API_MODE", raising=False)
    monkeypatch.setattr(identity, "resolve_identity",
                        lambda *a, **k: _identity())
    monkeypatch.setattr(identity, "dirty_with_async_refresh", lambda _: False)
    cache_calls = []
    monkeypatch.setattr(core, "get_cache_age_text",
                        lambda *a, **k: cache_calls.append((a, k)) or "COLD")
    payload = {
        "model": {"id": "k3", "display_name": "Kimi K3"},
        "cost": {"total_duration_ms": 279000},
        "rate_limits": {"five_hour": {"used_percentage": 12,
                                      "resets_at": 1999999999}},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    core.main(use_color=False, _suppress_side_effects=True)

    out = capsys.readouterr().out
    assert "❄ 4m39s" in out
    assert "⏱" not in out
    assert "COLD" not in out
    assert cache_calls == []
