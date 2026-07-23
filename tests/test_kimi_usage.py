"""Backend-aware 5h/7d limits: default (Anthropic subscription) vs kimi-k3."""
from claude_statusbar.core import _backend


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
