"""
run_niche()'s pre-filter must hand process_candidate() the candidate dicts
discovery produced, untouched.

This used to route every candidate through a pandas DataFrame purely to
compute one boolean column. Besides pulling in a heavyweight dependency
for a set-membership test, a round trip through a DataFrame rewrites the
data: the bookkeeping column comes back out in .to_dict("records"), and
any candidate missing a key another candidate has gets a NaN (which also
promotes that whole column's ints to floats).

No network: discovery, Airtable and enrichment are all monkeypatched.
"""


class _NullBlocklist:
    def match(self, handle="", email="", name=""):
        return ""


def _run_prefilter(monkeypatch, discovered, tracked_ids):
    """Run run_niche() far enough to capture what the pre-filter passed on."""
    import main

    seen = []

    monkeypatch.setattr(main, "count_added_today", lambda *a, **k: 0)
    monkeypatch.setattr(main, "run_discovery", lambda *a, **k: discovered)
    monkeypatch.setattr(
        main, "push_until_full",
        lambda candidates, *a, **k: seen.extend(candidates) or {
            "qualified": 0, "flagged": 0, "skipped": 0, "pushed_ids": set(),
        },
    )

    main.run_niche(
        niche_name="Home Theater",
        table_name="tbl",
        keywords=["kw"],
        max_results_per_keyword=5,
        days_back=7,
        globally_tracked_ids=tracked_ids,
        external_handles={},
        blocklist=_NullBlocklist(),
        niche_config={"min_avg_views": 10_000, "min_channel_age_months": 12},
        scraper=None,
    )
    return seen


def test_already_tracked_candidates_are_dropped(monkeypatch):
    discovered = [
        {"channel_id": "UC1", "channel_title": "Keep"},
        {"channel_id": "UC2", "channel_title": "Drop"},
    ]
    kept = _run_prefilter(monkeypatch, discovered, {"UC2"})

    assert [c["channel_id"] for c in kept] == ["UC1"]


def test_candidates_pass_through_unmodified(monkeypatch):
    """No bookkeeping keys may be bolted onto a candidate on the way past."""
    discovered = [{"channel_id": "UC1", "channel_title": "Keep", "matched_keywords": ["kw"]}]
    kept = _run_prefilter(monkeypatch, discovered, set())

    assert kept == [{"channel_id": "UC1", "channel_title": "Keep", "matched_keywords": ["kw"]}]


def test_ragged_candidates_keep_their_types(monkeypatch):
    """A key only some candidates have must not turn the others' ints into floats.

    discovery merges results across keywords, so a candidate found by one
    keyword can carry a field another candidate lacks.
    """
    discovered = [
        {"channel_id": "UC1", "view_count": 1234},
        {"channel_id": "UC2", "view_count": 5678, "extra": "only here"},
    ]
    kept = _run_prefilter(monkeypatch, discovered, set())

    assert [type(c["view_count"]) for c in kept] == [int, int]
    assert "extra" not in kept[0]


def test_no_candidates_is_not_an_error(monkeypatch):
    assert _run_prefilter(monkeypatch, [], set()) == []
