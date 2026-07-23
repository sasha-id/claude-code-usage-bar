"""Backend-aware 5h/7d limits: default (Anthropic subscription) vs kimi-k3."""
import json

import pytest

from claude_statusbar import core
from claude_statusbar.core import _backend, _fetch_kimi_usage


class TestBackend:
    def test_unset_base_url_is_default(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        assert _backend() == "default"

    def test_empty_base_url_is_default(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "  ")
        assert _backend() == "default"

    def test_kimi_host_is_kimi(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.kimi.com/coding")
        assert _backend() == "kimi"

    def test_kimi_apex_host_is_kimi(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://kimi.com/coding")
        assert _backend() == "kimi"

    def test_lookalike_host_is_not_kimi(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://notkimi.com/v1")
        assert _backend() == "other"

    def test_other_byo_host_is_other(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://litellm.ts.sasha.id")
        assert _backend() == "other"

    def test_unparseable_base_url_is_other(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "not a url")
        assert _backend() == "other"


# Verified live response shape (2026-07-23), values are strings by design.
LIVE_PAYLOAD = {
    "usage": {"limit": "100", "used": "12", "remaining": "88",
              "resetTime": "2026-07-29T14:51:08.099871Z"},
    "limits": [
        {"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
         "detail": {"limit": "100", "used": "16", "remaining": "84",
                    "resetTime": "2026-07-23T15:51:08.099871Z"}},
    ],
}


class _FakeResp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._raw


def _mock_urlopen(monkeypatch, payload):
    calls = []

    def fake(req, timeout=None):
        calls.append((req, timeout))
        return _FakeResp(payload)

    monkeypatch.setattr(core.urllib.request, "urlopen", fake)
    return calls


class TestFetchKimiUsage:
    def test_parses_both_windows(self, monkeypatch):
        calls = _mock_urlopen(monkeypatch, LIVE_PAYLOAD)
        out = _fetch_kimi_usage("https://api.kimi.com/coding/", "tok")
        assert out["seven_day"]["pct"] == pytest.approx(12.0)
        assert out["five_hour"]["pct"] == pytest.approx(16.0)
        # ISO resetTime converted to epoch seconds
        assert out["seven_day"]["resets_at"] == 1785336668
        assert out["five_hour"]["resets_at"] == 1784821868
        # request URL + headers
        req, timeout = calls[0]
        assert req.full_url == "https://api.kimi.com/coding/v1/usages"
        assert req.headers["Authorization"] == "Bearer tok"
        assert req.headers["User-agent"] == "KimiCLI/1.6"  # Request title-cases
        assert timeout == 2.5

    def test_missing_weekly_omits_seven_day(self, monkeypatch):
        _mock_urlopen(monkeypatch, {"limits": LIVE_PAYLOAD["limits"]})
        out = _fetch_kimi_usage("https://api.kimi.com/coding", "tok")
        assert "seven_day" not in out
        assert out["five_hour"]["pct"] == pytest.approx(16.0)

    def test_missing_5h_window_omits_five_hour(self, monkeypatch):
        _mock_urlopen(monkeypatch, {"usage": LIVE_PAYLOAD["usage"], "limits": []})
        out = _fetch_kimi_usage("https://api.kimi.com/coding", "tok")
        assert "five_hour" not in out
        assert out["seven_day"]["pct"] == pytest.approx(12.0)

    def test_malformed_window_is_omitted_not_fatal(self, monkeypatch):
        payload = {"usage": {"limit": "0", "used": "abc"},
                   "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                               "detail": {"limit": "100", "used": "16",
                                          "resetTime": "not-a-date"}}]}
        _mock_urlopen(monkeypatch, payload)
        assert _fetch_kimi_usage("https://api.kimi.com/coding", "tok") == {}
