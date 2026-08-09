"""
A transient YouTube error must not poison a keyword's search cache for
the rest of the UTC day (see _cache_key()). Caching a truncated/empty
result from a non-200 response or a hit quota ceiling would make a
healthy retry later the same day reuse that bad result instead of
re-querying — silently killing that keyword's discovery for up to ~15
hours with no error signal. See IMPORTANT 1 in the fix-wave review.
"""


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
    import discovery

    cache_file = tmp_path / "search_cache.json"
    monkeypatch.setattr(discovery, "SEARCH_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(discovery, "YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(discovery.time, "sleep", lambda s: None)
    monkeypatch.setattr(discovery, "record_spend", lambda *a, **k: None)
    monkeypatch.setattr(discovery, "can_afford_search", lambda: True)

    monkeypatch.setattr(discovery.requests, "get", lambda *a, **k: _Resp(503))

    result = discovery.discover_channels_by_keyword("kw", max_results=50)

    assert result == []
    cache = discovery._load_cache()
    key = discovery._cache_key("kw")
    assert key not in cache, "a 503 must not write a (poisoned) empty result into today's cache"


def test_healthy_call_after_a_non_200_re_queries_instead_of_reusing_the_cache(monkeypatch, tmp_path):
    """The regression this whole fix exists for: a 503 on the first call
    must not make a later healthy call in the same UTC day return the
    stale/empty cached result instead of actually searching again."""
    import discovery

    cache_file = tmp_path / "search_cache.json"
    monkeypatch.setattr(discovery, "SEARCH_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(discovery, "YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(discovery.time, "sleep", lambda s: None)
    monkeypatch.setattr(discovery, "record_spend", lambda *a, **k: None)
    monkeypatch.setattr(discovery, "can_afford_search", lambda: True)

    responses = [_Resp(503)]
    monkeypatch.setattr(discovery.requests, "get", lambda *a, **k: responses.pop(0))

    first = discovery.discover_channels_by_keyword("kw", max_results=50)
    assert first == []

    # A healthy response, now that the transient error has passed.
    responses.append(_Resp(200, _search_payload(["UC1", "UC2"])))
    second = discovery.discover_channels_by_keyword("kw", max_results=50)

    assert {c["channel_id"] for c in second} == {"UC1", "UC2"}

    # And it's now cached, so a third call costs nothing further.
    responses.append(_Resp(200, _search_payload(["SHOULD-NOT-BE-SEEN"])))
    third = discovery.discover_channels_by_keyword("kw", max_results=50)
    assert {c["channel_id"] for c in third} == {"UC1", "UC2"}


def test_quota_ceiling_hit_on_first_page_does_not_populate_the_cache(monkeypatch, tmp_path):
    """The can_afford_search() break at the top of the loop is the other
    place an incomplete result could get cached."""
    import discovery

    cache_file = tmp_path / "search_cache.json"
    monkeypatch.setattr(discovery, "SEARCH_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(discovery, "YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(discovery.time, "sleep", lambda s: None)
    monkeypatch.setattr(discovery, "record_spend", lambda *a, **k: None)
    monkeypatch.setattr(discovery, "can_afford_search", lambda: False)
    monkeypatch.setattr(
        discovery.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call search.list over the ceiling")),
    )

    result = discovery.discover_channels_by_keyword("kw", max_results=50)

    assert result == []
    cache = discovery._load_cache()
    assert discovery._cache_key("kw") not in cache


def test_clean_completion_still_caches_normally(monkeypatch, tmp_path):
    """Sanity check: the fix must not break caching on the ordinary
    successful path — only incomplete runs should skip the cache write."""
    import discovery

    cache_file = tmp_path / "search_cache.json"
    monkeypatch.setattr(discovery, "SEARCH_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(discovery, "YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(discovery.time, "sleep", lambda s: None)
    monkeypatch.setattr(discovery, "record_spend", lambda *a, **k: None)
    monkeypatch.setattr(discovery, "can_afford_search", lambda: True)
    monkeypatch.setattr(discovery.requests, "get", lambda *a, **k: _Resp(200, _search_payload(["UC1"])))

    result = discovery.discover_channels_by_keyword("kw", max_results=50)

    assert {c["channel_id"] for c in result} == {"UC1"}
    cache = discovery._load_cache()
    assert discovery._cache_key("kw") in cache
