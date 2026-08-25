"""
A transient YouTube error must not poison a keyword's search cache for
the rest of the Pacific day (see _cache_key()). Caching a truncated/empty
result from a non-200 response, a hit quota ceiling, or a network
exception would make a healthy retry later the same day reuse that bad
result instead of re-querying — silently killing that keyword's discovery
for up to ~24 hours with no error signal. See IMPORTANT 1 in the fix-wave
review.

Everything here mocks `discovery.HTTP.get` (the shared retrying session
from core/http_client.py), not `requests.get` — `discovery` no longer calls
`requests` directly, and tests/conftest.py hard-fails any request that
escapes to the real network.
"""
import pytest
import requests


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "error body"

    def json(self):
        return self._payload


def _search_payload(channel_ids):
    return {
        "items": [
            {"snippet": {"channelId": cid, "channelTitle": cid}} for cid in channel_ids
        ]
    }


def test_non_200_does_not_populate_the_cache(monkeypatch, tmp_path):
    from channel_vetting.discovery import youtube_search

    cache_file = tmp_path / "search_cache.json"
    monkeypatch.setattr(youtube_search, "SEARCH_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(youtube_search, "YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(youtube_search.time, "sleep", lambda s: None)
    monkeypatch.setattr(youtube_search, "record_spend", lambda *a, **k: None)
    monkeypatch.setattr(youtube_search, "can_afford_search", lambda: True)

    monkeypatch.setattr(youtube_search.HTTP, "get", lambda *a, **k: _Resp(503))

    result = youtube_search.discover_channels_by_keyword("kw", max_results=50)

    assert result == []
    cache = youtube_search._load_cache()
    key = youtube_search._cache_key("kw")
    assert key not in cache, "a 503 must not write a (poisoned) empty result into today's cache"


def test_healthy_call_after_a_non_200_re_queries_instead_of_reusing_the_cache(monkeypatch, tmp_path):
    """The regression this whole fix exists for: a 503 on the first call
    must not make a later healthy call in the same Pacific day return the
    stale/empty cached result instead of actually searching again."""
    from channel_vetting.discovery import youtube_search

    cache_file = tmp_path / "search_cache.json"
    monkeypatch.setattr(youtube_search, "SEARCH_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(youtube_search, "YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(youtube_search.time, "sleep", lambda s: None)
    monkeypatch.setattr(youtube_search, "record_spend", lambda *a, **k: None)
    monkeypatch.setattr(youtube_search, "can_afford_search", lambda: True)

    responses = [_Resp(503)]
    monkeypatch.setattr(youtube_search.HTTP, "get", lambda *a, **k: responses.pop(0))

    first = youtube_search.discover_channels_by_keyword("kw", max_results=50)
    assert first == []

    # A healthy response, now that the transient error has passed.
    responses.append(_Resp(200, _search_payload(["UC1", "UC2"])))
    second = youtube_search.discover_channels_by_keyword("kw", max_results=50)

    assert {c["channel_id"] for c in second} == {"UC1", "UC2"}

    # And it's now cached, so a third call costs nothing further.
    responses.append(_Resp(200, _search_payload(["SHOULD-NOT-BE-SEEN"])))
    third = youtube_search.discover_channels_by_keyword("kw", max_results=50)
    assert {c["channel_id"] for c in third} == {"UC1", "UC2"}


def test_quota_ceiling_hit_on_first_page_does_not_populate_the_cache(monkeypatch, tmp_path):
    """The can_afford_search() break at the top of the loop is the other
    place an incomplete result could get cached."""
    from channel_vetting.discovery import youtube_search

    cache_file = tmp_path / "search_cache.json"
    monkeypatch.setattr(youtube_search, "SEARCH_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(youtube_search, "YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(youtube_search.time, "sleep", lambda s: None)
    monkeypatch.setattr(youtube_search, "record_spend", lambda *a, **k: None)
    monkeypatch.setattr(youtube_search, "can_afford_search", lambda: False)
    monkeypatch.setattr(
        youtube_search.HTTP, "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call search.list over the ceiling")),
    )

    result = youtube_search.discover_channels_by_keyword("kw", max_results=50)

    assert result == []
    cache = youtube_search._load_cache()
    assert youtube_search._cache_key("kw") not in cache


def test_clean_completion_still_caches_normally(monkeypatch, tmp_path):
    """Sanity check: the fix must not break caching on the ordinary
    successful path — only incomplete runs should skip the cache write."""
    from channel_vetting.discovery import youtube_search

    cache_file = tmp_path / "search_cache.json"
    monkeypatch.setattr(youtube_search, "SEARCH_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(youtube_search, "YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(youtube_search.time, "sleep", lambda s: None)
    monkeypatch.setattr(youtube_search, "record_spend", lambda *a, **k: None)
    monkeypatch.setattr(youtube_search, "can_afford_search", lambda: True)
    monkeypatch.setattr(youtube_search.HTTP, "get", lambda *a, **k: _Resp(200, _search_payload(["UC1"])))

    result = youtube_search.discover_channels_by_keyword("kw", max_results=50)

    assert {c["channel_id"] for c in result} == {"UC1"}
    cache = youtube_search._load_cache()
    assert youtube_search._cache_key("kw") in cache


def test_network_exception_does_not_raise_and_does_not_cache(monkeypatch, tmp_path):
    """A ConnectionError/ReadTimeout used to propagate all the way out of
    run_discovery() and kill the whole run — every niche lost, not just this
    keyword. It must now be handled like any other cut-short search: no
    exception, and (critically) no cache entry, so a healthy retry later the
    same Pacific day actually re-queries."""
    from channel_vetting.discovery import youtube_search

    cache_file = tmp_path / "search_cache.json"
    monkeypatch.setattr(youtube_search, "SEARCH_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(youtube_search, "YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(youtube_search.time, "sleep", lambda s: None)
    monkeypatch.setattr(youtube_search, "record_spend", lambda *a, **k: None)
    monkeypatch.setattr(youtube_search, "can_afford_search", lambda: True)

    def boom(*a, **k):
        raise requests.ConnectionError("network is down")

    monkeypatch.setattr(youtube_search.HTTP, "get", boom)

    result = youtube_search.discover_channels_by_keyword("kw", max_results=50)

    assert result == []
    assert youtube_search._cache_key("kw") not in youtube_search._load_cache()


def test_healthy_call_after_a_network_exception_re_queries(monkeypatch, tmp_path):
    """The cache-poisoning guard on the new exception path, end to end."""
    from channel_vetting.discovery import youtube_search

    cache_file = tmp_path / "search_cache.json"
    monkeypatch.setattr(youtube_search, "SEARCH_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(youtube_search, "YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(youtube_search.time, "sleep", lambda s: None)
    monkeypatch.setattr(youtube_search, "record_spend", lambda *a, **k: None)
    monkeypatch.setattr(youtube_search, "can_afford_search", lambda: True)

    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ReadTimeout("too slow")
        return _Resp(200, _search_payload(["UC1", "UC2"]))

    monkeypatch.setattr(youtube_search.HTTP, "get", flaky)

    assert youtube_search.discover_channels_by_keyword("kw", max_results=50) == []
    second = youtube_search.discover_channels_by_keyword("kw", max_results=50)
    assert {c["channel_id"] for c in second} == {"UC1", "UC2"}


def test_record_spend_not_called_on_non_200(monkeypatch, tmp_path):
    """A 403 quotaExceeded costs Google 0 units. Charging it 100 (which the
    old ordering did, calling record_spend before the status check) shrank
    can_afford_search()'s own headroom for spend that never happened."""
    from channel_vetting.discovery import youtube_search

    cache_file = tmp_path / "search_cache.json"
    monkeypatch.setattr(youtube_search, "SEARCH_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(youtube_search, "YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(youtube_search.time, "sleep", lambda s: None)
    monkeypatch.setattr(youtube_search, "can_afford_search", lambda: True)

    spends = []
    monkeypatch.setattr(youtube_search, "record_spend", lambda units, **k: spends.append(units))
    monkeypatch.setattr(youtube_search.HTTP, "get", lambda *a, **k: _Resp(403))

    youtube_search.discover_channels_by_keyword("kw", max_results=50)

    assert spends == [], "a non-200 was never billed, so it must not be recorded"


def test_record_spend_not_called_on_network_exception(monkeypatch, tmp_path):
    """A request that never reached Google was never billed either."""
    from channel_vetting.discovery import youtube_search

    cache_file = tmp_path / "search_cache.json"
    monkeypatch.setattr(youtube_search, "SEARCH_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(youtube_search, "YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(youtube_search.time, "sleep", lambda s: None)
    monkeypatch.setattr(youtube_search, "can_afford_search", lambda: True)

    spends = []
    monkeypatch.setattr(youtube_search, "record_spend", lambda units, **k: spends.append(units))

    def boom(*a, **k):
        raise requests.ConnectionError("network is down")

    monkeypatch.setattr(youtube_search.HTTP, "get", boom)

    youtube_search.discover_channels_by_keyword("kw", max_results=50)

    assert spends == []


def test_record_spend_is_called_on_a_served_200(monkeypatch, tmp_path):
    """The other half of the ordering fix: moving record_spend after the
    status check must not stop it being called on the path that DID cost 100
    units, or the ceiling check goes blind."""
    from channel_vetting.discovery import youtube_search

    cache_file = tmp_path / "search_cache.json"
    monkeypatch.setattr(youtube_search, "SEARCH_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(youtube_search, "YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(youtube_search.time, "sleep", lambda s: None)
    monkeypatch.setattr(youtube_search, "can_afford_search", lambda: True)

    spends = []
    monkeypatch.setattr(youtube_search, "record_spend", lambda units, **k: spends.append(units))
    monkeypatch.setattr(youtube_search.HTTP, "get", lambda *a, **k: _Resp(200, _search_payload(["UC1"])))

    youtube_search.discover_channels_by_keyword("kw", max_results=50)

    assert spends == [youtube_search.QUOTA_COST_SEARCH_LIST]


def test_params_never_carry_the_api_key(monkeypatch, tmp_path):
    """The key travels as the shared session's X-goog-api-key header. In
    `params` it lands in every requests exception message — and in CI, in an
    Actions log kept for 90 days. See core/http_client.py's module docstring."""
    from channel_vetting.discovery import youtube_search

    cache_file = tmp_path / "search_cache.json"
    monkeypatch.setattr(youtube_search, "SEARCH_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(youtube_search, "YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(youtube_search.time, "sleep", lambda s: None)
    monkeypatch.setattr(youtube_search, "record_spend", lambda *a, **k: None)
    monkeypatch.setattr(youtube_search, "can_afford_search", lambda: True)

    seen = {}

    def capture(url, params=None, **k):
        seen["params"] = params
        return _Resp(200, _search_payload(["UC1"]))

    monkeypatch.setattr(youtube_search.HTTP, "get", capture)

    youtube_search.discover_channels_by_keyword("kw", max_results=50, days_back=7)

    assert "key" not in seen["params"]
    assert "fake-key" not in str(seen["params"])
    # Sanity: the rest of the request is still intact.
    assert seen["params"]["q"] == "kw"
    assert "publishedAfter" in seen["params"]


def test_cache_key_uses_the_pacific_day_not_utc(monkeypatch):
    """02:00 UTC is still the PREVIOUS day in Pacific Time. The cache day and
    the quota day must roll together — when this keyed on UTC, a run in that
    7-8 hour window got a fresh cache day (re-searching every keyword at 100
    units each) charged against a Pacific quota day already part-spent."""
    from datetime import datetime, timezone

    from channel_vetting.discovery import youtube_search
    from channel_vetting.budget import quota_tracker

    # 2026-08-11 02:00 UTC == 2026-08-10 19:00 America/Los_Angeles.
    frozen_utc = datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_utc.astimezone(tz) if tz else frozen_utc

    monkeypatch.setattr(quota_tracker, "datetime", _FrozenDatetime)

    assert quota_tracker.today_pacific() == "2026-08-10"
    assert frozen_utc.strftime("%Y-%m-%d") == "2026-08-11", "the two clocks must differ here"

    # discovery imported the helper by name, so it sees the same frozen clock.
    assert youtube_search._cache_key("kw", max_results=50, days_back=7) == "2026-08-10::7d::n50::kw"
    assert youtube_search._cache_key("kw").startswith(quota_tracker.today_pacific())


def test_cache_key_normalises_the_keyword():
    """Casing/whitespace must not mint a second 100-unit cache miss."""
    from channel_vetting.discovery import youtube_search

    assert youtube_search._cache_key("  Home Theater ") == youtube_search._cache_key("home theater")


def test_a_wider_days_back_is_not_served_from_a_narrower_cached_result(monkeypatch, tmp_path):
    """
    CLAUDE.md's documented escape hatch for a thin day is `--days-back 90`.
    While the key ignored days_back, the daily cron's 7-day result satisfied
    it — so the sweep returned the narrow window and spent nothing, with no
    error and no way to tell from the output.
    """
    from channel_vetting.discovery import youtube_search

    monkeypatch.setattr(youtube_search, "SEARCH_CACHE_FILE", str(tmp_path / "search_cache.json"))
    monkeypatch.setattr(youtube_search, "YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(youtube_search.time, "sleep", lambda s: None)
    monkeypatch.setattr(youtube_search, "record_spend", lambda *a, **k: None)
    monkeypatch.setattr(youtube_search, "can_afford_search", lambda: True)

    responses = [_Resp(200, _search_payload(["NARROW"]))]
    monkeypatch.setattr(youtube_search.HTTP, "get", lambda *a, **k: responses.pop(0))

    narrow = youtube_search.discover_channels_by_keyword("kw", max_results=50, days_back=7)
    assert {c["channel_id"] for c in narrow} == {"NARROW"}

    # The 90-day sweep must actually search rather than reuse the 7-day rows.
    responses.append(_Resp(200, _search_payload(["WIDE1", "WIDE2"])))
    wide = youtube_search.discover_channels_by_keyword("kw", max_results=50, days_back=90)
    assert {c["channel_id"] for c in wide} == {"WIDE1", "WIDE2"}

    # ...and each window keeps its own entry, so neither re-spends.
    assert youtube_search._cache_key("kw", 50, 7) in youtube_search._load_cache()
    assert youtube_search._cache_key("kw", 50, 90) in youtube_search._load_cache()


def test_a_test_mode_result_does_not_satisfy_a_full_run(monkeypatch, tmp_path):
    """`--test` caches max_results=5; a later full run must still ask for 50."""
    from channel_vetting.discovery import youtube_search

    monkeypatch.setattr(youtube_search, "SEARCH_CACHE_FILE", str(tmp_path / "search_cache.json"))
    monkeypatch.setattr(youtube_search, "YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(youtube_search.time, "sleep", lambda s: None)
    monkeypatch.setattr(youtube_search, "record_spend", lambda *a, **k: None)
    monkeypatch.setattr(youtube_search, "can_afford_search", lambda: True)

    responses = [_Resp(200, _search_payload(["SMOKE"]))]
    monkeypatch.setattr(youtube_search.HTTP, "get", lambda *a, **k: responses.pop(0))

    youtube_search.discover_channels_by_keyword("kw", max_results=5, days_back=7)

    responses.append(_Resp(200, _search_payload([f"UC{i}" for i in range(4)])))
    full = youtube_search.discover_channels_by_keyword("kw", max_results=50, days_back=7)
    assert len(full) == 4


def test_save_cache_is_atomic_and_leaves_no_tmp_file(monkeypatch, tmp_path):
    """An interrupted json.dump must not destroy the cache it was rewriting —
    a truncated file reads back as {} and re-spends 100 units per keyword."""
    from channel_vetting.discovery import youtube_search

    cache_file = tmp_path / "search_cache.json"
    monkeypatch.setattr(youtube_search, "SEARCH_CACHE_FILE", str(cache_file))

    original = {"2026-08-10::kw": [{"channel_id": "UC1"}]}
    youtube_search._save_cache(original)
    assert not list(tmp_path.glob("*.tmp"))

    def exploding_dump(*a, **k):
        raise OSError("disk full half-way through")

    # A context, not a bare setattr + undo(): the `monkeypatch` fixture is
    # shared with conftest's autouse block_real_http guard, so an undo() here
    # would quietly disarm the no-real-HTTP safety net too.
    with monkeypatch.context() as m:
        m.setattr(youtube_search.json, "dump", exploding_dump)
        with pytest.raises(OSError):
            youtube_search._save_cache({"2026-08-10::other": []})
        assert not list(tmp_path.glob("*.tmp")), "a stale .tmp must not be left behind"

    assert youtube_search._load_cache() == original


# --- quota_tracker's log write ------------------------------------------
#
# These live in this file rather than one of their own because they pin the
# SAME failure mode the tests above pin: a JSON file truncated part-way
# through a rewrite reads back as {}, and the pipeline then believes
# something it has already paid for is free. For the search cache that costs
# 100 wasted units per keyword; for the quota log it FAILS OPEN and hands out
# a whole fresh QUOTA_CEILING on top of the day's real spend.


def test_save_log_is_atomic_and_leaves_no_tmp_file(monkeypatch, tmp_path):
    """The fail-open one. If _save_log() truncates and dies, _load_log()
    returns {} == "0 spent today", and can_afford_search() authorises a
    second full ceiling. The pre-existing log must survive an interrupted
    write, and no .tmp may be left lying around."""
    from channel_vetting.budget import quota_tracker

    log_file = tmp_path / "quota_log.json"
    monkeypatch.setattr(quota_tracker, "QUOTA_LOG_FILE", str(log_file))

    today = quota_tracker.today_pacific()
    quota_tracker._save_log({today: 4200})
    assert not list(tmp_path.glob("*.tmp"))
    assert quota_tracker.get_today_spend() == 4200

    def exploding_dump(*a, **k):
        raise OSError("killed mid-write")

    with monkeypatch.context() as m:
        m.setattr(quota_tracker.json, "dump", exploding_dump)
        with pytest.raises(OSError):
            quota_tracker._save_log({today: 9999})
        assert not list(tmp_path.glob("*.tmp")), "a stale .tmp must not be left behind"

    # The load must still see the real spend, NOT the fail-open 0.
    assert quota_tracker.get_today_spend() == 4200


def test_a_truncated_log_would_fail_open_which_is_what_atomicity_prevents(monkeypatch, tmp_path):
    """Documents the consequence the atomic write exists to avoid, so the
    reason for the .tmp dance can't be lost in a future cleanup."""
    from channel_vetting.budget import quota_tracker

    log_file = tmp_path / "quota_log.json"
    monkeypatch.setattr(quota_tracker, "QUOTA_LOG_FILE", str(log_file))
    # Pinned rather than inherited from .env, so a machine with a small
    # QUOTA_CEILING can't make this pass for the wrong reason.
    monkeypatch.setattr(quota_tracker, "QUOTA_CEILING", 8000)

    # What a half-finished json.dump leaves behind.
    log_file.write_text('{"2026-08-10": 79', encoding="utf-8")

    assert quota_tracker._load_log() == {}
    assert quota_tracker.get_today_spend() == 0
    assert quota_tracker.can_afford_search() is True, (
        "this is the fail-open behaviour: a corrupt log reads as an unspent day"
    )


def test_save_log_prunes_old_days_but_keeps_today(monkeypatch, tmp_path):
    """One key per Pacific day accumulates forever otherwise, in a file
    rewritten in full on every record_spend()."""
    from datetime import datetime, timedelta

    from channel_vetting.budget import quota_tracker

    log_file = tmp_path / "quota_log.json"
    monkeypatch.setattr(quota_tracker, "QUOTA_LOG_FILE", str(log_file))

    today_str = quota_tracker.today_pacific()
    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    ancient = (today - timedelta(days=90)).strftime("%Y-%m-%d")
    just_past_retention = (
        today - timedelta(days=quota_tracker.LOG_RETENTION_DAYS + 1)
    ).strftime("%Y-%m-%d")

    quota_tracker._save_log({
        ancient: 100,
        just_past_retention: 200,
        yesterday: 300,
        today_str: 400,
    })

    written = quota_tracker._load_log()
    assert today_str in written
    assert yesterday in written
    assert ancient not in written
    assert just_past_retention not in written


def test_pruning_keeps_keys_it_cannot_parse(monkeypatch, tmp_path):
    """The log is hand-inspectable; silently deleting a key we don't
    understand is worse than carrying it."""
    from channel_vetting.budget import quota_tracker

    log_file = tmp_path / "quota_log.json"
    monkeypatch.setattr(quota_tracker, "QUOTA_LOG_FILE", str(log_file))

    today = quota_tracker.today_pacific()
    quota_tracker._save_log({today: 10, "note-from-a-human": "investigating"})

    assert quota_tracker._load_log()["note-from-a-human"] == "investigating"


def test_record_spend_round_trips_through_the_atomic_write(monkeypatch, tmp_path):
    """End-to-end: the write path the whole pipeline uses still accumulates."""
    from channel_vetting.budget import quota_tracker

    log_file = tmp_path / "quota_log.json"
    monkeypatch.setattr(quota_tracker, "QUOTA_LOG_FILE", str(log_file))

    assert quota_tracker.record_spend(100, call_name="search.list") == 100
    assert quota_tracker.record_spend(2, call_name="videos.list") == 102
    assert quota_tracker.get_today_spend() == 102
    assert not list(tmp_path.glob("*.tmp"))


def test_today_pacific_is_public_and_the_private_alias_still_resolves():
    """discovery/youtube_search.py shares this exact helper so the cache day and the quota
    day roll together. The old private name is kept as an alias."""
    from channel_vetting.budget import quota_tracker

    assert callable(quota_tracker.today_pacific)
    assert quota_tracker._today_pacific is quota_tracker.today_pacific
