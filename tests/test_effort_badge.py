"""Compact model-attached effort/thinking/fast badge in every look."""
import io
import json
import sys

import pytest

from claude_statusbar import styles
from claude_statusbar.themes import get_theme


def _render(style, **changes):
    kwargs = dict(
        msgs_pct=12, weekly_pct=20, reset_5h="1h00m", reset_7d="2d03h",
        model="Kimi K3(350.0k/1.0M)", ctx_pct=35,
        effort_level="xhigh", thinking_enabled=True, fast_mode=True,
        use_color=False, theme=get_theme("graphite"),
    )
    kwargs.update(changes)
    return styles.render(style, **kwargs)


@pytest.mark.parametrize("style,bar_glyph,cell", [
    ("aurora", "block", "■"),
    ("aurora", "thin", "━"),
])
def test_aurora_badge_sits_between_model_and_context_bar(style, bar_glyph, cell):
    out = _render(style, bar_glyph=bar_glyph)

    assert f"Kimi K3 ✳xhigh↯ {cell}" in out
    assert "35% · 350.0k/1.0M" in out
    assert "Kimi K3(" not in out
    assert "effort:" not in out
    assert len(out.splitlines()) == 1


@pytest.mark.parametrize("style,bar_glyph,cell", [
    ("aurora", "block", "■"),
    ("aurora", "thin", "━"),
])
def test_aurora_narrow_layout_keeps_context_totals(style, bar_glyph, cell):
    out = _render(style, bar_glyph=bar_glyph, max_width=55,
                  narrow_layout="stack")

    assert "35% · 350.0k/1.0M" in out
    assert f"Kimi K3 ✳xhigh↯ {cell}" in out


def test_classic_badge_follows_context_suffix():
    out = _render("classic")

    assert "Kimi K3(350.0k/1.0M) ✳xhigh↯" in out
    assert "effort:" not in out
    assert len(out.splitlines()) == 1


def test_classic_no_quota_model_keeps_badge():
    out = _render("classic", no_quota=True, model="Kimi K3")

    assert "Kimi K3 ✳xhigh↯" in out


def test_badge_omits_absent_fields_and_abbreviates_medium():
    out = _render("aurora", model="Kimi K3", effort_level="medium",
                  thinking_enabled=False, fast_mode=False, ctx_pct=None)
    assert "Kimi K3 med" in out
    assert "✳" not in out and "↯" not in out

    out = _render("aurora", model="Kimi K3", effort_level="",
                  thinking_enabled=False, fast_mode=True, ctx_pct=None)
    assert "Kimi K3 ↯" in out


def test_thinking_badge_does_not_require_an_effort_label():
    out = _render("aurora", model="Kimi K3", effort_level="",
                  thinking_enabled=True, fast_mode=False, ctx_pct=None)
    assert "Kimi K3 ✳" in out

    out = _render("aurora", model="Kimi K3", effort_level="",
                  thinking_enabled=True, fast_mode=True, ctx_pct=None)
    assert "Kimi K3 ✳↯" in out


def test_badge_uses_static_dusty_effort_color_and_muted_glyphs():
    theme = get_theme("graphite")
    badge = styles._effort_badge(
        "xhigh", True, True, theme=theme, use_color=True)

    assert styles._fg(styles._EFFORT_GRADIENTS["xhigh"][-1]) in badge
    assert f"{styles._fg(theme.mute)}✳" in badge
    assert f"{styles._fg(theme.mute)}↯" in badge
    assert "\x1b[2m" not in badge


def test_main_passes_live_mode_fields_without_enabling_mode_line(
        tmp_path, monkeypatch, capsys):
    from claude_statusbar import config, core
    path = tmp_path / ".claude" / "claude-statusbar.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "style": "aurora", "bar_glyph": "block", "show_mode": False,
        "show_project_branch": False, "show_cache_age": False,
        "show_todos": False, "show_fp_risk": False, "show_per_model": False,
    }))
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("CS_API_MODE", raising=False)
    payload = {
        "model": {"id": "k3", "display_name": "Kimi K3"},
        "effort": {"level": "xhigh"},
        "thinking": {"enabled": True}, "fast_mode": True,
        "rate_limits": {"five_hour": {"used_percentage": 12,
                                      "resets_at": 1999999999}},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    core.main(use_color=False, _suppress_side_effects=True)

    out = capsys.readouterr().out
    assert "Kimi K3 ✳xhigh↯" in out
    assert "effort:" not in out
