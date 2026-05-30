from claude_statusbar.progress import build_bar

def test_bar_zero_percent():
    assert build_bar(0, 10) == "░░░░░░░░░░"

def test_bar_fifty_percent():
    assert build_bar(50, 10) == "█████░░░░░"

def test_bar_100_percent():
    assert build_bar(100, 10) == "██████████"

def test_bar_over_100():
    assert build_bar(120, 10) == "██████████"

def test_bar_small_nonzero_rounds_up():
    """1% should show at least 1 filled block."""
    assert build_bar(1, 10) == "█░░░░░░░░░"

def test_bar_25_percent():
    """25% -> int(2.5 + 0.5) = 3 blocks (always rounds half-up, not banker's)."""
    assert build_bar(25, 10) == "███░░░░░░░"

def test_bar_15_percent():
    """15% -> int(1.5 + 0.5) = 2 blocks."""
    assert build_bar(15, 10) == "██░░░░░░░░"

def test_bar_boundary_values():
    """Test at various boundaries to confirm half-up rounding."""
    assert build_bar(5, 10) == "█░░░░░░░░░"   # int(0.5+0.5)=1
    assert build_bar(45, 10) == "█████░░░░░"   # int(4.5+0.5)=5
    assert build_bar(99, 10) == "██████████"    # int(9.9+0.5)=10

from claude_statusbar.progress import (
    color_for_percent,
    colorize,
    normalize_thresholds,
    GREEN,
    YELLOW,
    RED,
    RESET,
)

def test_color_safe():
    assert color_for_percent(20) == GREEN

def test_color_warning():
    assert color_for_percent(50) == YELLOW

def test_color_critical():
    assert color_for_percent(80) == RED

def test_color_boundary_30():
    assert color_for_percent(30) == YELLOW

def test_color_boundary_70():
    assert color_for_percent(70) == RED

def test_color_custom_thresholds():
    assert color_for_percent(39, warning_threshold=40, critical_threshold=80) == GREEN
    assert color_for_percent(40, warning_threshold=40, critical_threshold=80) == YELLOW
    assert color_for_percent(80, warning_threshold=40, critical_threshold=80) == RED

def test_normalize_thresholds_rejects_invalid_ranges():
    try:
        normalize_thresholds(80, 40)
    except ValueError as exc:
        assert "warning < critical" in str(exc)
    else:
        raise AssertionError("Expected invalid thresholds to raise ValueError")

def test_colorize():
    result = colorize("hello", RED)
    assert result == f"{RED}hello{RESET}"

def test_colorize_no_color():
    result = colorize("hello", RED, use_color=False)
    assert result == "hello"

from claude_statusbar.progress import format_status_line

def test_format_status_line_basic():
    line = format_status_line(
        msgs_pct=82, tkns_pct=None,
        reset_time="2h51m", model="Opus 4.6",
        weekly_pct=45,
        use_color=False,
    )
    assert "5h[" in line
    assert "7d[" in line
    assert "2h51m" in line
    assert "Opus 4.6" in line

def test_format_status_line_over_100():
    line = format_status_line(
        msgs_pct=105, tkns_pct=None,
        reset_time="0h03m", model="Opus 4.6",
        weekly_pct=100,
        use_color=False,
    )
    assert "5h[" in line
    assert "MAX" in line

def test_format_status_line_no_data():
    line = format_status_line(
        msgs_pct=None, tkns_pct=None,
        reset_time="--", model="unknown",
        weekly_pct=None,
        use_color=False,
    )
    assert "5h[" in line
    assert "7d[" in line
    assert "--%" in line

def test_format_status_line_bypass():
    line = format_status_line(
        msgs_pct=50, tkns_pct=None,
        reset_time="3h00m", model="Sonnet",
        weekly_pct=20,
        bypass=True, use_color=False,
    )
    assert "BYPASS" in line

def test_format_status_line_7d_countdown():
    """7d countdown should appear next to the 7d progress bar."""
    line = format_status_line(
        msgs_pct=50, tkns_pct=None,
        reset_time="2h30m", model="Opus 4.6",
        weekly_pct=30, reset_time_7d="3d05h",
        use_color=False,
    )
    assert "7d[" in line
    assert "3d05h" in line
    assert "2h30m" in line

def test_format_status_line_7d_no_countdown():
    """When reset_time_7d is empty, no extra reset-time token after 7d bar."""
    line = format_status_line(
        msgs_pct=50, tkns_pct=None,
        reset_time="2h30m", model="Opus 4.6",
        weekly_pct=30, reset_time_7d="",
        use_color=False,
    )
    assert "7d[" in line
    # 5h renders its reset time; 7d does not (no reset_time_7d given)
    assert "2h30m" in line

def test_format_status_line_with_color():
    """Verify ANSI codes are present when use_color=True."""
    line = format_status_line(
        msgs_pct=80, tkns_pct=None,
        reset_time="1h00m", model="Opus",
        weekly_pct=30,
        use_color=True,
    )
    assert "\033[" in line
    assert "\033[0m" in line


from claude_statusbar.progress import (
    build_effort_segment,
    THINKING_GLYPH,
    FAST_GLYPH,
)


def test_effort_segment_empty_when_nothing():
    assert build_effort_segment(None, False, False, use_color=False) == ""


def test_effort_segment_plain_level():
    assert build_effort_segment("high", thinking_enabled=False,
                                fast_mode=False, use_color=False) == "high"


def test_effort_segment_medium_abbreviates():
    assert build_effort_segment("medium", thinking_enabled=False,
                                fast_mode=False, use_color=False) == "med"


def test_effort_segment_thinking_prefixes_glyph():
    seg = build_effort_segment("xhigh", thinking_enabled=True,
                               fast_mode=False, use_color=False)
    assert seg == f"{THINKING_GLYPH}xhigh"


def test_effort_segment_fast_mode_appends_glyph():
    seg = build_effort_segment("max", thinking_enabled=True,
                               fast_mode=True, use_color=False)
    assert seg == f"{THINKING_GLYPH}max{FAST_GLYPH}"


def test_effort_segment_fast_without_effort_still_shows():
    """Fast mode shows even when the model reports no effort level."""
    assert build_effort_segment(None, thinking_enabled=False,
                                fast_mode=True, use_color=False) == FAST_GLYPH


def test_format_status_line_includes_effort():
    line = format_status_line(
        msgs_pct=50, tkns_pct=None,
        reset_time="2h30m", model="Opus 4.8",
        weekly_pct=30, use_color=False,
        effort_level="xhigh", thinking_enabled=True, fast_mode=False,
    )
    assert f"Opus 4.8 {THINKING_GLYPH}xhigh" in line


def test_format_status_line_no_effort_leaves_model_clean():
    line = format_status_line(
        msgs_pct=50, tkns_pct=None,
        reset_time="2h30m", model="Opus 4.8",
        weekly_pct=30, use_color=False,
    )
    assert "Opus 4.8" in line
    assert THINKING_GLYPH not in line
    assert FAST_GLYPH not in line
