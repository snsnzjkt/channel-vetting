"""Discovery must stop spending 100-unit searches once it has enough."""


def _fake_keyword_results(keyword):
    """Three fresh channels per keyword, named after it."""
    return [
        {"channel_id": f"{keyword}-{i}", "channel_title": f"{keyword} {i}", "matched_keywords": [keyword]}
        for i in range(3)
    ]


def test_stops_once_target_fresh_is_met(monkeypatch):
    import discovery

    searched = []

    def fake_search(keyword, max_results=50, days_back=90):
        searched.append(keyword)
        return _fake_keyword_results(keyword)

    monkeypatch.setattr(discovery, "discover_channels_by_keyword", fake_search)
    monkeypatch.setattr(discovery.time, "sleep", lambda s: None)

    discovery.run_discovery(["a", "b", "c", "d"], target_fresh=5)
    assert searched == ["a", "b"]  # 6 fresh after two keywords; c and d never searched


def test_searches_all_keywords_when_no_target(monkeypatch):
    import discovery

    searched = []

    def fake_search(keyword, max_results=50, days_back=90):
        searched.append(keyword)
        return _fake_keyword_results(keyword)

    monkeypatch.setattr(discovery, "discover_channels_by_keyword", fake_search)
    monkeypatch.setattr(discovery.time, "sleep", lambda s: None)

    discovery.run_discovery(["a", "b", "c"])
    assert searched == ["a", "b", "c"]


def test_excluded_ids_do_not_count_toward_target(monkeypatch):
    """Already-tracked channels aren't fresh, so discovery must keep going."""
    import discovery

    searched = []

    def fake_search(keyword, max_results=50, days_back=90):
        searched.append(keyword)
        return _fake_keyword_results(keyword)

    monkeypatch.setattr(discovery, "discover_channels_by_keyword", fake_search)
    monkeypatch.setattr(discovery.time, "sleep", lambda s: None)

    exclude = {f"a-{i}" for i in range(3)}
    discovery.run_discovery(["a", "b", "c"], exclude_ids=exclude, target_fresh=3)
    assert searched == ["a", "b"]


def test_matched_keywords_still_merge(monkeypatch):
    import discovery

    def fake_search(keyword, max_results=50, days_back=90):
        return [{"channel_id": "shared", "channel_title": "Shared", "matched_keywords": [keyword]}]

    monkeypatch.setattr(discovery, "discover_channels_by_keyword", fake_search)
    monkeypatch.setattr(discovery.time, "sleep", lambda s: None)

    result = discovery.run_discovery(["a", "b"])
    assert result[0]["matched_keywords"] == ["a", "b"]
