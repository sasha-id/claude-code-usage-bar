"""Kimi coding-plan quota parsing, caching, and backend integration."""
import io
import json
import subprocess
import sys

import pytest


def _kimi():
    from claude_statusbar import kimi_usage
    return kimi_usage


def _cache():
    from claude_statusbar import kimi_cache
    return kimi_cache


def _refresh():
    from claude_statusbar import _kimi_refresh
    return _kimi_refresh


def _core():
    from claude_statusbar import core
    return core


VALID_LIMITS = {
    "five_hour": {"pct": 16.0, "resets_at": 1784821868},
    "seven_day": {"pct": 12.0, "resets_at": 1785336668},
}


# Verified live response shape (2026-07-23); numeric fields are strings.
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

    monkeypatch.setattr(_kimi().urllib.request, "build_opener",
                        lambda *handlers: _FakeOpener())
    return calls


def test_fetch_parses_both_quota_windows(monkeypatch):
    calls = _mock_urlopen(monkeypatch, LIVE_PAYLOAD)

    out = _kimi().fetch_usage("https://api.kimi.com/coding/", "tok")

    assert out["seven_day"]["pct"] == pytest.approx(12.0)
    assert out["five_hour"]["pct"] == pytest.approx(16.0)
    assert out["seven_day"]["resets_at"] == 1785336668
    assert out["five_hour"]["resets_at"] == 1784821868
    req, timeout = calls[0]
    assert req.full_url == "https://api.kimi.com/coding/v1/usages"
    assert req.headers["Authorization"] == "Bearer tok"
    assert timeout == 2.5


def test_fetch_omits_malformed_window_without_dropping_valid_one(monkeypatch):
    payload = {
        "usage": LIVE_PAYLOAD["usage"],
        "limits": [{"window": {"duration": 300,
                               "timeUnit": "TIME_UNIT_MINUTE"},
                    "detail": {"limit": "100", "used": "NaN",
                               "resetTime": "not-a-date"}}],
    }
    _mock_urlopen(monkeypatch, payload)

    out = _kimi().fetch_usage("https://api.kimi.com/coding", "tok")

    assert "five_hour" not in out
    assert out["seven_day"]["pct"] == pytest.approx(12.0)


def test_fetch_preserves_over_limit_percentage(monkeypatch):
    payload = {"usage": {**LIVE_PAYLOAD["usage"], "used": "120"},
               "limits": []}
    _mock_urlopen(monkeypatch, payload)

    out = _kimi().fetch_usage("https://api.kimi.com/coding", "tok")

    assert out["seven_day"]["pct"] == pytest.approx(120.0)


def test_insecure_kimi_url_never_sends_bearer_token(monkeypatch):
    calls = _mock_urlopen(monkeypatch, LIVE_PAYLOAD)

    with pytest.raises(ValueError, match="HTTPS"):
        _kimi().fetch_usage("http://api.kimi.com/coding", "tok")

    assert calls == []


def test_fetch_does_not_follow_redirects(monkeypatch):
    handlers = []

    class _Opener:
        def open(self, req, timeout):
            return _FakeResp(LIVE_PAYLOAD)

    def build_opener(*installed):
        handlers.extend(installed)
        return _Opener()

    kimi = _kimi()
    monkeypatch.setattr(kimi.urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(kimi.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("urlopen must not be used"))

    kimi.fetch_usage("https://api.kimi.com/coding", "tok")

    assert len(handlers) == 1
    assert isinstance(handlers[0], kimi._NoRedirectHandler)
    assert handlers[0].redirect_request(
        None, None, 302, "Found", {}, "https://example.invalid/collect") is None


def test_cached_limits_are_account_scoped_and_secret_free(tmp_path, monkeypatch):
    cache = _cache()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path)
    fp = cache.fingerprint("https://api.kimi.com/coding", "synthetic-token")
    cache.write_cache_atomic(fp, {"ts": 100, "limits": VALID_LIMITS})

    live = {"ANTHROPIC_BASE_URL": "https://api.kimi.com/coding",
            "ANTHROPIC_AUTH_TOKEN": "synthetic-token"}
    assert cache.cached_limits(live) == VALID_LIMITS
    assert cache.cached_limits({**live, "ANTHROPIC_AUTH_TOKEN": "other"}) is None
    assert cache.cached_limits({**live, "ANTHROPIC_BASE_URL":
                                "https://other.kimi.com/coding"}) is None
    assert cache.cached_limits({"CS_KIMI_FP": fp}) == VALID_LIMITS
    assert "synthetic-token" not in cache.cache_path_for(fp).read_text()


def test_stale_limits_remain_renderable_but_not_fresh(tmp_path, monkeypatch):
    cache = _cache()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path)
    fp = "account"
    cache.write_cache_atomic(fp, {"ts": 100, "limits": VALID_LIMITS})

    entry = cache.read_cache(fp)
    assert cache.cached_limits({"CS_KIMI_FP": fp}) == VALID_LIMITS
    assert cache.is_fresh(entry, now=100 + cache.TTL_SECONDS - 1)
    assert not cache.is_fresh(entry, now=100 + cache.TTL_SECONDS)


def test_failed_refresh_backoff_uses_attempt_time(tmp_path, monkeypatch):
    cache = _cache()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path)
    cache.write_cache_atomic("account", {
        "ts": 100, "attempted_at": 500, "refresh_failed": True,
        "limits": VALID_LIMITS,
    })

    entry = cache.read_cache("account")
    assert cache.is_fresh(entry, now=529)
    assert not cache.is_fresh(entry, now=530)
    assert cache.cached_limits({"CS_KIMI_FP": "account"}) == VALID_LIMITS


def test_invalid_cached_window_is_dropped(tmp_path, monkeypatch):
    cache = _cache()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path)
    cache.cache_path_for("partial").write_text(json.dumps({
        "ts": 100,
        "limits": {"five_hour": {"pct": "16", "resets_at": 1784821868},
                   "seven_day": VALID_LIMITS["seven_day"]},
    }))
    cache.cache_path_for("invalid").write_text(json.dumps({
        "ts": 100, "limits": {"five_hour": {"pct": True, "resets_at": -1}},
    }))

    assert cache.cached_limits({"CS_KIMI_FP": "partial"}) == {
        "seven_day": VALID_LIMITS["seven_day"]}
    assert cache.cached_limits({"CS_KIMI_FP": "invalid"}) is None


def test_detached_refresh_writes_only_quota_data(tmp_path, monkeypatch):
    cache = _cache()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path)
    monkeypatch.setattr(_kimi(), "fetch_usage", lambda base, token: VALID_LIMITS)
    fp = cache.fingerprint("https://api.kimi.com/coding", "synthetic-token")

    assert _refresh().refresh("https://api.kimi.com/coding",
                              "synthetic-token", fp) == 0

    entry = cache.read_cache(fp)
    assert entry["limits"] == VALID_LIMITS
    assert entry["ts"] > 0
    assert "synthetic-token" not in cache.cache_path_for(fp).read_text()


def test_failed_refresh_preserves_stale_limits(tmp_path, monkeypatch):
    cache = _cache()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path)
    cache.write_cache_atomic("account", {"ts": 100, "limits": VALID_LIMITS})
    def fail(base, token):
        raise OSError("offline")
    monkeypatch.setattr(_kimi(), "fetch_usage", fail)
    monkeypatch.setattr(_refresh().time, "time", lambda: 500)

    assert _refresh().refresh("https://api.kimi.com/coding", "token", "account") == 0

    entry = cache.read_cache("account")
    assert entry["limits"] == VALID_LIMITS
    assert entry["ts"] == 100
    assert entry["attempted_at"] == 500
    assert entry["refresh_failed"] is True


def test_refresh_lock_prevents_duplicate_network_fetch(tmp_path, monkeypatch):
    from claude_statusbar import git_cache
    cache = _cache()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path)
    monkeypatch.setattr(git_cache, "cache_path_for",
                        lambda name: tmp_path / f"{name}.json")
    monkeypatch.setattr(_kimi(), "fetch_usage",
                        lambda *args: pytest.fail("duplicate fetch"))
    lock = git_cache.try_claim("kimi:account")
    try:
        assert _refresh().refresh("https://api.kimi.com/coding", "token", "account") == 0
    finally:
        lock.close()


def test_initial_failed_refresh_has_retry_backoff(tmp_path, monkeypatch):
    cache = _cache()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path)
    monkeypatch.setattr(_kimi(), "fetch_usage",
                        lambda *args: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(_refresh().time, "time", lambda: 500)

    assert _refresh().refresh("https://api.kimi.com/coding", "token", "account") == 0

    entry = cache.read_cache("account")
    assert cache.cached_limits({"CS_KIMI_FP": "account"}) is None
    assert cache.is_fresh(entry, now=529)
    assert not cache.is_fresh(entry, now=530)


def test_failed_cache_write_keeps_refresh_inflight(tmp_path, monkeypatch):
    from claude_statusbar import git_cache
    cache = _cache()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path)
    monkeypatch.setattr(git_cache, "cache_path_for",
                        lambda name: tmp_path / f"{name}.json")
    monkeypatch.setattr(_kimi(), "fetch_usage", lambda base, token: VALID_LIMITS)
    monkeypatch.setattr(cache, "write_cache_atomic", lambda fp, entry: False)
    cache.mark_inflight("account")

    assert _refresh().refresh("https://api.kimi.com/coding", "token", "account") == 0

    assert cache.is_inflight("account")


def test_inflight_marker_lifecycle(tmp_path, monkeypatch):
    cache = _cache()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path)

    assert not cache.is_inflight("account")
    cache.mark_inflight("account")
    assert cache.is_inflight("account")
    cache.clear_inflight("account")
    assert not cache.is_inflight("account")


def test_stale_limits_render_immediately_and_spawn_only_detached_refresh(
        tmp_path, monkeypatch):
    cache = _cache()
    core = _core()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path)
    base = "https://api.kimi.com/coding"
    fp = cache.fingerprint(base, "synthetic-token")
    cache.write_cache_atomic(fp, {"ts": 100, "limits": VALID_LIMITS})
    calls = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(_kimi(), "fetch_usage",
                        lambda *args: pytest.fail("synchronous network fetch"))

    env = {"ANTHROPIC_BASE_URL": base, "ANTHROPIC_AUTH_TOKEN": "synthetic-token"}
    assert core.kimi_limits(env) == VALID_LIMITS
    assert core.kimi_limits(env) == VALID_LIMITS  # inflight suppresses a duplicate

    assert len(calls) == 1
    args, kwargs = calls[0]
    argv = args[0]
    assert argv[1:3] == ["-m", "claude_statusbar._kimi_refresh"]
    assert argv[3] == base
    assert "synthetic-token" not in argv
    assert kwargs["env"]["CS_KIMI_TOKEN"] == "synthetic-token"
    assert kwargs["env"]["CS_KIMI_FP"] == fp
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] is subprocess.DEVNULL


def test_kimi_refresh_respects_fresh_read_only_and_daemon_modes(
        tmp_path, monkeypatch):
    cache = _cache()
    core = _core()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path)
    base = "https://api.kimi.com/coding"
    fp = cache.fingerprint(base, "token")
    calls = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *args, **kwargs: calls.append((args, kwargs)))

    cache.write_cache_atomic(fp, {"ts": 100, "limits": VALID_LIMITS})
    assert core.kimi_limits({"CS_KIMI_FP": fp,
                             "ANTHROPIC_BASE_URL": base}) == VALID_LIMITS
    assert core.kimi_limits({"ANTHROPIC_BASE_URL": base,
                             "ANTHROPIC_AUTH_TOKEN": "token"}, spawn=False) == VALID_LIMITS
    assert calls == []

    import time
    cache.write_cache_atomic(fp, {"ts": time.time(), "limits": VALID_LIMITS})
    assert core.kimi_limits({"ANTHROPIC_BASE_URL": base,
                             "ANTHROPIC_AUTH_TOKEN": "token"}) == VALID_LIMITS
    assert calls == []


def test_persistent_worker_uses_background_pool(tmp_path, monkeypatch):
    from claude_statusbar import identity, refresh_pool
    cache = _cache()
    core = _core()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path)
    monkeypatch.setattr(identity, "_BACKGROUND_COLLECTORS", True)
    calls = []
    monkeypatch.setattr(refresh_pool, "submit",
                        lambda *args: calls.append(args))
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *args, **kwargs: pytest.fail("unexpected child"))
    env = {"ANTHROPIC_BASE_URL": "https://api.kimi.com/coding",
           "ANTHROPIC_API_KEY": "token"}

    assert core.kimi_limits(env) is None
    assert len(calls) == 1
    key, func, base, token, fp = calls[0]
    assert key == ("kimi", fp)
    assert base == env["ANTHROPIC_BASE_URL"]
    assert token == "token"
    assert func.__name__ == "refresh"


def test_kimi_refresh_rejects_non_kimi_and_insecure_urls(monkeypatch):
    core = _core()
    calls = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    for base in ("https://relay.example", "http://api.kimi.com/coding",
                 "https://api.kimi.com.evil.example"):
        assert core.kimi_limits({"ANTHROPIC_BASE_URL": base,
                                 "ANTHROPIC_AUTH_TOKEN": "token"}) is None
    assert calls == []


def test_kimi_is_quota_capable_unless_explicitly_overridden():
    core = _core()
    env = {"ANTHROPIC_BASE_URL": "https://api.kimi.com/coding"}
    assert not core.is_no_quota_mode(env)
    assert core.is_no_quota_mode(env, override="on")
    assert not core.is_no_quota_mode(env, override="off")
    assert core.is_no_quota_mode({"ANTHROPIC_BASE_URL": "https://relay.example"})


def test_parse_uses_kimi_cache_not_anthropic_payload_or_cache(
        tmp_path, monkeypatch):
    from claude_statusbar import quota_cache
    cache = _cache()
    core = _core()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path)
    base = "https://api.kimi.com/coding"
    fp = cache.fingerprint(base, "token")
    cache.write_cache_atomic(fp, {"ts": 100, "limits": VALID_LIMITS})
    monkeypatch.setattr(quota_cache, "remember",
                        lambda *args: pytest.fail("must not seed Anthropic quota"))
    monkeypatch.setattr(quota_cache, "read",
                        lambda *args: pytest.fail("must not read Anthropic quota"))
    payload = {
        "_cs_env": {"ANTHROPIC_BASE_URL": base, "CS_KIMI_FP": fp},
        "rate_limits": {"five_hour": {"used_percentage": 99, "resets_at": 1}},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    result = core.parse_stdin_data()

    assert result["_kimi_quota"] is True
    assert result["rate_limit_pct"] == 16.0
    assert result["rate_limit_resets_at"] == 1784821868
    assert result["rate_limit_7d_pct"] == 12.0
    assert result["rate_limit_7d_resets_at"] == 1785336668


def _write_main_config(tmp_path, monkeypatch, **values):
    from claude_statusbar import config
    path = tmp_path / ".claude" / "claude-statusbar.json"
    path.parent.mkdir(exist_ok=True)
    payload = {"style": "classic", "show_project_branch": False,
               "show_cache_age": False, "show_todos": False,
               "show_projection": False, "show_forecast": False,
               "show_fp_risk": False, "show_per_model": False}
    payload.update(values)
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setenv("HOME", str(tmp_path))


def test_main_renders_kimi_without_anthropic_prediction(
        tmp_path, monkeypatch, capsys):
    from claude_statusbar import predict
    cache = _cache()
    core = _core()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path / "kimi")
    _write_main_config(tmp_path, monkeypatch,
                       show_projection=True, show_forecast=True,
                       show_per_model=True)
    from claude_statusbar import scoped_usage
    monkeypatch.setattr(scoped_usage, "cached_limits",
                        lambda *a, **k: pytest.fail("Anthropic scoped quota used"))
    base = "https://api.kimi.com/coding"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", base)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token")
    monkeypatch.delenv("CS_API_MODE", raising=False)
    fp = cache.fingerprint(base, "token")
    cache.write_cache_atomic(fp, {"ts": 100, "limits": VALID_LIMITS})
    for name in ("reconcile_account", "projection", "forecast"):
        monkeypatch.setattr(predict, name,
                            lambda *a, **k: pytest.fail("Anthropic prediction used"))
    scheduled = []
    monkeypatch.setattr(core, "kimi_limits",
                        lambda env: scheduled.append(env) or cache.cached_limits(env))
    payload = {"session_id": "kimi", "model": {"id": "k3", "display_name": "Kimi K3"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    core.main(use_color=False, _suppress_side_effects=True)

    out = capsys.readouterr().out
    assert "Kimi K3" in out
    assert "5h " in out and "16%" in out
    assert "7d " in out and "12%" in out
    assert "ctx " not in out
    assert len(scheduled) == 1


def test_main_kimi_without_cache_waits_without_stale_warning(
        tmp_path, monkeypatch, capsys):
    from claude_statusbar import predict
    cache = _cache()
    core = _core()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path / "kimi")
    _write_main_config(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.kimi.com/coding")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token")
    monkeypatch.delenv("CS_API_MODE", raising=False)
    monkeypatch.setattr(core, "kimi_limits", lambda env: None)
    monkeypatch.setattr(core, "_transcript_has_assistant", lambda path: True)
    monkeypatch.setattr(core, "_no_quota_heuristic",
                        lambda *a, **k: pytest.fail("Kimi used relay heuristic"))
    monkeypatch.setattr(predict, "quota_cache_status",
                        lambda: pytest.fail("Anthropic stale state used"))
    payload = {"session_id": "kimi", "version": "2.1.90",
               "transcript_path": "/session.jsonl",
               "model": {"id": "k3", "display_name": "Kimi K3"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    core.main(use_color=False, _suppress_side_effects=True)

    out = capsys.readouterr().out
    assert "Kimi K3" in out
    assert "stale" not in out
    assert "ctx[" not in out


def test_explicit_no_quota_override_disables_kimi_refresh(
        tmp_path, monkeypatch, capsys):
    cache = _cache()
    core = _core()
    monkeypatch.setattr(cache, "_cache_root", lambda: tmp_path / "kimi")
    _write_main_config(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.kimi.com/coding")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token")
    monkeypatch.setenv("CS_API_MODE", "on")
    monkeypatch.setattr(core, "kimi_limits",
                        lambda env: pytest.fail("quota refresh in no-quota mode"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "model": {"id": "k3", "display_name": "Kimi K3"},
        "context_window": {"context_window_size": 1000000,
                           "used_percentage": 35,
                           "total_input_tokens": 350000},
    })))

    core.main(use_color=False, _suppress_side_effects=True)

    out = capsys.readouterr().out
    assert "ctx" in out
    assert "350.0k/1.0M" in out
    assert "5h" not in out


def test_thin_client_stamps_secret_free_kimi_fingerprint(monkeypatch):
    from claude_statusbar import render_thin
    core = _core()
    cache = _cache()
    base = "https://api.kimi.com/coding"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", base)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-primary")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-fallback")
    calls = []
    monkeypatch.setattr(core, "kimi_limits",
                        lambda env: calls.append(env))
    monkeypatch.setattr(core, "relay_balance",
                        lambda env: pytest.fail("Kimi must not probe relay balance"))

    stamped = render_thin._inject_session_env(b'{"session_id":"kimi"}')

    session_env = json.loads(stamped)["_cs_env"]
    assert session_env["CS_KIMI_FP"] == cache.fingerprint(base, "synthetic-primary")
    assert b"synthetic-primary" not in stamped
    assert b"synthetic-fallback" not in stamped
    assert len(calls) == 1


@pytest.mark.parametrize("source", ["env", "config"])
def test_thin_client_no_quota_never_refreshes_kimi(
        tmp_path, monkeypatch, source):
    from claude_statusbar import render_thin
    core = _core()
    cache = _cache()
    base = "https://api.kimi.com/coding"
    _write_main_config(tmp_path, monkeypatch,
                       api_mode="on" if source == "config" else "auto")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", base)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic")
    if source == "env":
        monkeypatch.setenv("CS_API_MODE", "on")
    else:
        monkeypatch.delenv("CS_API_MODE", raising=False)
    monkeypatch.setattr(core, "kimi_limits",
                        lambda env: pytest.fail("quota refresh in no-quota mode"))

    stamped = render_thin._inject_session_env(b'{"session_id":"kimi"}')

    session_env = json.loads(stamped)["_cs_env"]
    assert session_env["CS_KIMI_FP"] == cache.fingerprint(base, "synthetic")
    assert b"synthetic" not in stamped
