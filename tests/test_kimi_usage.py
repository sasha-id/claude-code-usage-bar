"""Backend-aware 5h/7d limits: default (Anthropic subscription) vs kimi-k3."""
import io
import json
import os
import sys
import time
from threading import Event, Thread

import pytest

from claude_statusbar import core
from claude_statusbar.core import (
    _backend,
    _fetch_kimi_usage,
    _kimi_usage,
    parse_stdin_data,
)


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

    def test_http_kimi_host_is_not_kimi(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://api.kimi.com/coding")
        assert _backend() == "other"

    def test_lookalike_host_is_not_kimi(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://notkimi.com/v1")
        assert _backend() == "other"

    def test_other_byo_host_is_other(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://litellm.ts.sasha.id")
        assert _backend() == "other"

    def test_unparseable_base_url_is_other(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "not a url")
        assert _backend() == "other"

    def test_malformed_base_url_is_other(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://[")
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

    class _FakeOpener:
        def open(self, req, timeout=None):
            calls.append((req, timeout))
            return _FakeResp(payload)

    monkeypatch.setattr(core.urllib.request, "build_opener",
                        lambda *handlers: _FakeOpener())
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

    def test_non_dict_window_entry_is_skipped(self, monkeypatch):
        payload = {"usage": LIVE_PAYLOAD["usage"],
                   "limits": [{"window": "oops", "detail": {}}]}
        _mock_urlopen(monkeypatch, payload)
        out = _fetch_kimi_usage("https://api.kimi.com/coding", "tok")
        assert "five_hour" not in out
        assert out["seven_day"]["pct"] == pytest.approx(12.0)

    def test_http_url_never_sends_bearer_token(self, monkeypatch):
        calls = _mock_urlopen(monkeypatch, LIVE_PAYLOAD)
        with pytest.raises(ValueError, match="HTTPS"):
            _fetch_kimi_usage("http://api.kimi.com/coding", "tok")
        assert calls == []

    def test_redirect_handler_rejects_follow_up(self):
        handler = core._NoRedirectHandler()
        assert handler.redirect_request(
            None, None, 302, "Found", {}, "https://example.invalid/collect"
        ) is None

    def test_fetch_installs_no_redirect_handler(self, monkeypatch):
        handlers = []

        class _Opener:
            def open(self, req, timeout=None):
                return _FakeResp(LIVE_PAYLOAD)

        def build_opener(*installed):
            handlers.extend(installed)
            return _Opener()

        monkeypatch.setattr(core.urllib.request, "build_opener", build_opener)
        monkeypatch.setattr(
            core.urllib.request, "urlopen",
            lambda *args, **kwargs: pytest.fail("urlopen must not be used"),
        )

        _fetch_kimi_usage("https://api.kimi.com/coding", "tok")

        assert len(handlers) == 1
        assert isinstance(handlers[0], core._NoRedirectHandler)

    def test_non_finite_api_values_are_omitted(self, monkeypatch):
        payload = {
            "usage": {"limit": "NaN", "used": "12",
                      "resetTime": LIVE_PAYLOAD["usage"]["resetTime"]},
            "limits": [
                {"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                 "detail": {"limit": "100", "used": "Infinity",
                            "resetTime": LIVE_PAYLOAD["limits"][0]["detail"]["resetTime"]}},
            ],
        }
        _mock_urlopen(monkeypatch, payload)
        assert _fetch_kimi_usage("https://api.kimi.com/coding", "tok") == {}

    def test_over_limit_percentage_is_preserved(self, monkeypatch):
        payload = {"usage": {**LIVE_PAYLOAD["usage"], "used": "120"},
                   "limits": []}
        _mock_urlopen(monkeypatch, payload)
        out = _fetch_kimi_usage("https://api.kimi.com/coding", "tok")
        assert out["seven_day"]["pct"] == pytest.approx(120.0)


GOOD = {"five_hour": {"pct": 16.0, "resets_at": 1784821868},
        "seven_day": {"pct": 12.0, "resets_at": 1785336668}}


class TestKimiUsageCache:
    def _write_cache(self, path, age_s, limits=GOOD, base="b", token="t"):
        path.write_text(json.dumps(
            {"cache_key": core._kimi_cache_key(base, token),
             "fetched_at": time.time() - age_s, "limits": limits}))

    def test_fresh_cache_skips_fetch(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        self._write_cache(cache, age_s=10)

        def boom(base, tok):
            raise AssertionError("fetch must not run inside the TTL")

        assert _kimi_usage("b", "t", cache_path=cache, fetcher=boom) == GOOD

    def test_stale_cache_fetches_and_rewrites(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        self._write_cache(cache, age_s=3600, limits={"stale": True})
        new = {"five_hour": {"pct": 1.0, "resets_at": 2}}
        out = _kimi_usage("b", "t", cache_path=cache, fetcher=lambda b, t: new)
        assert out == new
        written = json.loads(cache.read_text())
        assert written["limits"] == new
        assert written["fetched_at"] > time.time() - 60

    def test_fetch_error_serves_stale_cache(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        self._write_cache(cache, age_s=86400)  # any age — errors keep it

        def fail(base, tok):
            raise OSError("timeout")

        assert _kimi_usage("b", "t", cache_path=cache, fetcher=fail) == GOOD

    def test_fetch_error_without_cache_returns_empty_and_throttles(self, tmp_path):
        fetches = []

        def fail(base, tok):
            fetches.append(True)
            raise OSError("timeout")

        cache = tmp_path / "none.json"
        assert _kimi_usage("b", "t", cache_path=cache, fetcher=fail) == {}
        assert _kimi_usage("b", "t", cache_path=cache, fetcher=fail) == {}
        assert fetches == [True]

    def test_missing_token_never_fetches(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        self._write_cache(cache, age_s=86400, token="present")

        def boom(base, tok):
            raise AssertionError("fetch must not run without a token")

        assert _kimi_usage("b", "", cache_path=cache, fetcher=boom) == {}

    def test_failed_cache_write_still_throttles_refresh(self, monkeypatch, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        new = {"five_hour": {"pct": 1.0, "resets_at": 2}}
        fetches = []

        def fetch(base, token):
            fetches.append(True)
            return new

        def fail_replace(src, dst):
            raise OSError("read-only cache")

        monkeypatch.setattr(core.os, "replace", fail_replace)

        assert _kimi_usage("b", "t", cache_path=cache,
                           fetcher=fetch) == new
        assert _kimi_usage("b", "t", cache_path=cache,
                           fetcher=fetch) == {}
        assert fetches == [True]

    def test_refresh_throttle_is_scoped_to_url_and_token(
            self, monkeypatch, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        fetches = []

        def fetch(base, token):
            fetches.append((base, token))
            return {"five_hour": {"pct": 1.0, "resets_at": 2}}

        def fail_replace(src, dst):
            raise OSError("read-only cache")

        monkeypatch.setattr(core.os, "replace", fail_replace)

        _kimi_usage("old", "same", cache_path=cache, fetcher=fetch)
        _kimi_usage("new", "same", cache_path=cache, fetcher=fetch)

        assert fetches == [("old", "same"), ("new", "same")]

    def test_corrupt_cache_is_treated_as_absent(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        cache.write_text("not json{{")

        def fail(base, tok):
            raise OSError("down")

        assert _kimi_usage("b", "t", cache_path=cache, fetcher=fail) == {}

    def test_non_numeric_fetched_at_treated_as_stale(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        cache.write_text(json.dumps({
            "cache_key": core._kimi_cache_key("b", "t"),
            "fetched_at": "junk", "limits": GOOD}))
        new = {"five_hour": {"pct": 1.0, "resets_at": 2}}
        out = _kimi_usage("b", "t", cache_path=cache, fetcher=lambda b, t: new)
        assert out == new

    def test_impossible_fetched_at_is_treated_as_stale(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        cache.write_text(json.dumps({
            "cache_key": core._kimi_cache_key("b", "t"),
            "fetched_at": 10 ** 1000, "limits": GOOD}))
        new = {"five_hour": {"pct": 1.0, "resets_at": 2}}
        out = _kimi_usage("b", "t", cache_path=cache, fetcher=lambda b, t: new)
        assert out == new

    def test_non_dict_limits_treated_as_absent(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        cache.write_text(json.dumps({
            "cache_key": core._kimi_cache_key("b", "t"),
            "fetched_at": time.time(), "limits": [1, 2]}))

        def fail(base, tok):
            raise OSError("down")

        assert _kimi_usage("b", "t", cache_path=cache, fetcher=fail) == {}

    def test_future_fetched_at_is_treated_as_stale(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        self._write_cache(cache, age_s=-3600)
        new = {"five_hour": {"pct": 1.0, "resets_at": 2}}
        assert _kimi_usage("b", "t", cache_path=cache,
                           fetcher=lambda b, t: new) == new

    def test_future_refresh_lease_is_reclaimable(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        lock = cache.with_name(f"{cache.name}.lock")
        lock.write_text("old-lease")
        future = time.time() + 3600
        os.utime(lock, (future, future))

        lease = core._claim_file_lease(cache, 10)

        assert lease is not None
        core._release_file_lease(lease)

    def test_cache_is_scoped_to_token(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        self._write_cache(cache, age_s=10, token="old")
        new = {"five_hour": {"pct": 1.0, "resets_at": 2}}
        assert _kimi_usage("b", "new", cache_path=cache,
                           fetcher=lambda b, t: new) == new

    def test_cache_is_scoped_to_url_with_same_token(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        self._write_cache(cache, age_s=10, base="old", token="same")
        new = {"five_hour": {"pct": 1.0, "resets_at": 2}}
        assert _kimi_usage("new", "same", cache_path=cache,
                           fetcher=lambda b, t: new) == new

    def test_incomplete_fresh_cache_omits_only_invalid_window(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        self._write_cache(cache, age_s=10, limits={
            "five_hour": {"pct": 16.0},
            "seven_day": GOOD["seven_day"],
        })

        def boom(base, tok):
            raise AssertionError("valid cached window must not fetch")

        assert _kimi_usage("b", "t", cache_path=cache, fetcher=boom) == {
            "seven_day": GOOD["seven_day"],
        }

    def test_impossible_reset_epoch_is_omitted_from_fresh_cache(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        self._write_cache(cache, age_s=10, token="", limits={
            "five_hour": {"pct": 16.0, "resets_at": 10 ** 1000},
            "seven_day": GOOD["seven_day"],
        })

        assert _kimi_usage("b", "", cache_path=cache) == {
            "seven_day": GOOD["seven_day"],
        }

    def test_concurrent_stale_cache_refresh_uses_one_fetch(self, tmp_path):
        cache = tmp_path / "kimi_usage.json"
        self._write_cache(cache, age_s=3600)
        fetch_started = Event()
        allow_first_fetch_to_finish = Event()
        first_result = []
        second_fetches = []

        def first_fetch(base, token):
            fetch_started.set()
            assert allow_first_fetch_to_finish.wait(timeout=1)
            return {"five_hour": {"pct": 1.0, "resets_at": 2}}

        def second_fetch(base, token):
            second_fetches.append(True)
            return {"five_hour": {"pct": 2.0, "resets_at": 3}}

        thread = Thread(target=lambda: first_result.append(
            _kimi_usage("b", "t", cache_path=cache, fetcher=first_fetch)))
        thread.start()
        assert fetch_started.wait(timeout=1)
        assert _kimi_usage("b", "t", cache_path=cache,
                           fetcher=second_fetch) == GOOD
        allow_first_fetch_to_finish.set()
        thread.join(timeout=1)

        assert not thread.is_alive()
        assert first_result == [{"five_hour": {"pct": 1.0, "resets_at": 2}}]
        assert second_fetches == []


def _run_parse(monkeypatch, tmp_path, payload, env):
    """parse_stdin_data against a synthetic stdin payload + env. HOME redirects
    the in-function last_stdin.json cache into tmp_path; CTX_STATE_FILE (a
    module-level constant) is patched explicitly so _smooth_context can't
    clobber the real session's state file."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(core, "CTX_STATE_FILE", tmp_path / "ctx_state.json")
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    return parse_stdin_data()


STDIN_RL = {"rate_limits": {
    "five_hour": {"used_percentage": 42.0, "resets_at": 1784821868},
    "seven_day": {"used_percentage": 7.0, "resets_at": 1785336668}}}


class TestParseStdinData:
    def test_default_backend_reads_stdin_rate_limits(self, monkeypatch, tmp_path):
        out = _run_parse(monkeypatch, tmp_path, STDIN_RL, env={})
        assert out["rate_limit_pct"] == 42.0
        assert out["rate_limit_7d_pct"] == 7.0

    def test_kimi_backend_uses_kimi_usage(self, monkeypatch, tmp_path):
        monkeypatch.setattr(core, "_kimi_usage", lambda b, t: GOOD)
        out = _run_parse(monkeypatch, tmp_path, {"model": {}}, env={
            "ANTHROPIC_BASE_URL": "https://api.kimi.com/coding",
            "ANTHROPIC_AUTH_TOKEN": "tok"})
        assert out["rate_limit_pct"] == 16.0
        assert out["rate_limit_resets_at"] == 1784821868
        assert out["rate_limit_7d_pct"] == 12.0
        assert out["rate_limit_7d_resets_at"] == 1785336668

    def test_kimi_backend_skips_stale_anthropic_fallback(self, monkeypatch, tmp_path):
        # A previous default-backend session left Anthropic numbers in the cache…
        cache = tmp_path / ".cache" / "claude-statusbar"
        cache.mkdir(parents=True)
        (cache / "last_stdin.json").write_text(json.dumps(STDIN_RL))
        # …and Kimi has no quota data yet: the bar must show NOTHING, not the
        # stale Anthropic numbers (today's bug).
        monkeypatch.setattr(core, "_kimi_usage", lambda b, t: {})
        out = _run_parse(monkeypatch, tmp_path, {"model": {}}, env={
            "ANTHROPIC_BASE_URL": "https://api.kimi.com/coding",
            "ANTHROPIC_AUTH_TOKEN": "tok"})
        assert "rate_limit_pct" not in out
        assert "rate_limit_7d_pct" not in out

    def test_default_backend_keeps_cache_fallback(self, monkeypatch, tmp_path):
        cache = tmp_path / ".cache" / "claude-statusbar"
        cache.mkdir(parents=True)
        (cache / "last_stdin.json").write_text(json.dumps(STDIN_RL))
        out = _run_parse(monkeypatch, tmp_path, {"model": {}}, env={})
        assert out["rate_limit_pct"] == 42.0

    def test_kimi_rate_limits_do_not_seed_default_cache(self, monkeypatch, tmp_path):
        monkeypatch.setattr(core, "_kimi_usage", lambda b, t: GOOD)
        _run_parse(monkeypatch, tmp_path, STDIN_RL, env={
            "ANTHROPIC_BASE_URL": "https://api.kimi.com/coding",
            "ANTHROPIC_AUTH_TOKEN": "tok"})

        out = _run_parse(monkeypatch, tmp_path, {"model": {}}, env={})
        assert "rate_limit_pct" not in out
        assert "rate_limit_7d_pct" not in out
