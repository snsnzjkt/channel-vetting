"""
run_niche() must keep discovering until the daily budget is FILLED, not
until a fixed multiple of it has been discovered.

The 2026-08 criteria change turned three requirements (view floor, video
count, search zone) into hard discards in pre_push_drop_reason(). Measured
against live YouTube search results, only ~15% of fresh candidates now
survive to become a row. Discovery banked
`(headroom) * CANDIDATE_OVERSHOOT` = 1.5x candidates and then stopped
searching for good, so a 40-row budget was fed 60 candidates and produced
~9 rows — the caps were unreachable by construction, not by bad luck.

These tests pin the refill loop: while budget remains AND keywords remain,
discover another batch. No network: discovery, Airtable and enrichment are
all monkeypatched.
"""
from channel_vetting import pipeline
from channel_vetting.discovery.search_zones import ZONE_CORE


class _NullBlocklist:
    def match(self, handle="", email="", name=""):
        return ""


def _fake_discovery(searched, per_keyword=20):
    """
    Stand-in for run_discovery: `per_keyword` unique candidates each.

    Faithfully reproduces the real function's early stop — after each
    keyword, if `target_fresh` fresh candidates are banked it stops and
    leaves the remaining keywords unsearched. A stub that ignored
    target_fresh would silently bypass the very behaviour under test.
    """

    def run_discovery(keywords, max_results_per_keyword=50, days_back=90,
                      exclude_ids=None, target_fresh=None):
        exclude_ids = exclude_ids or set()
        merged = {}
        for keyword in keywords:
            searched.append(keyword)
            for i in range(per_keyword):
                cid = f"{keyword}-{i}"
                merged[cid] = {"channel_id": cid, "channel_title": cid,
                               "matched_keywords": [keyword]}
            if target_fresh is not None and len(set(merged) - exclude_ids) >= target_fresh:
                break
        return [c for cid, c in merged.items() if cid not in exclude_ids]

    return run_discovery


def _survives_one_in(n):
    """process_candidate stand-in: every nth candidate becomes a Qualified row."""
    seen = {"n": 0}

    def process_candidate(candidate, *a, **k):
        seen["n"] += 1
        if seen["n"] % n:
            return None, "below_view_minimum"
        return {"Channel ID": candidate["channel_id"], "Qualification": "Qualified"}, "Qualified"

    return process_candidate


def _run(monkeypatch, keywords, per_keyword=20, survives_one_in=5,
         qualified_today=0, flagged_today=0):
    searched = []
    pushed = []

    monkeypatch.setattr(pipeline, "run_discovery", _fake_discovery(searched, per_keyword))
    monkeypatch.setattr(pipeline, "process_candidate", _survives_one_in(survives_one_in))
    monkeypatch.setattr(pipeline, "push_record", lambda t, r: pushed.append(r) or True)
    # count_added_today(table) -> total; count_added_today(table, QUALIFIED) -> qualified
    monkeypatch.setattr(
        pipeline, "count_added_today",
        lambda table, qualification=None: qualified_today if qualification
        else qualified_today + flagged_today,
    )

    discovered, processed, pushed_ids, cap_ok = pipeline.run_niche(
        niche_name="Home Theater",
        table_name="tbl",
        keywords=keywords,
        max_results_per_keyword=50,
        days_back=7,
        globally_tracked_ids=set(),
        external_handles={},
        blocklist=_NullBlocklist(),
        niche_config={"min_avg_views": 10_000, "min_channel_age_months": 12, "allowed_country_codes": ZONE_CORE},
        scraper=None,
    )
    return searched, pushed, processed, cap_ok


def test_keeps_searching_until_the_qualified_budget_is_full(monkeypatch):
    """
    The regression: 1 row per 5 candidates, a full day's budget wanted, 10
    keywords available.

    Asserted against DAILY_QUALIFIED_CAP rather than a literal. These tests are
    about the refill LOOP — that it keeps searching until the budget is full —
    and hardcoding the cap made six of them fail when the cap was raised, which
    tested the constant rather than the behaviour.
    """
    # The point is that ONE keyword cannot fill the budget, so the loop has to
    # come back for more. Sized off the cap: ~1/8 of the budget per keyword, so
    # roughly 8 keywords are needed whatever the cap is set to. A fixed
    # per_keyword would silently stop testing refill the moment the cap moved.
    cap = pipeline.DAILY_QUALIFIED_CAP
    survives = 5
    per_keyword = max(survives, (cap // 8) * survives)
    searched, pushed, processed, _ = _run(
        monkeypatch, [f"kw{i}" for i in range(10)],
        per_keyword=per_keyword, survives_one_in=survives,
    )

    assert len(pushed) == cap
    assert processed == cap
    assert len(searched) > 3, (
        f"only searched {searched} — discovery stopped at a fixed multiple of "
        "the headroom instead of refilling until the budget was full"
    )


def test_stops_as_soon_as_the_budget_is_full(monkeypatch):
    """A generous survival rate must NOT spend quota on every keyword."""
    cap = pipeline.DAILY_QUALIFIED_CAP
    searched, pushed, _, _ = _run(
        monkeypatch, [f"kw{i}" for i in range(10)],
        per_keyword=cap * 2, survives_one_in=1,
    )

    assert len(pushed) == cap
    assert len(searched) <= 3, f"searched {searched} — should have stopped once full"


def test_an_unfillable_flagged_budget_does_not_burn_every_keyword(monkeypatch):
    """
    Lifestyle Sofa's shape: min_channel_age_months is None, so qualify() can
    only ever return "Qualified" and no flagged row is possible. The flagged
    budget is a ceiling, not a target — the loop must not keep searching for
    rows that cannot exist.
    """
    cap = pipeline.DAILY_QUALIFIED_CAP
    searched, pushed, _, _ = _run(
        monkeypatch, [f"kw{i}" for i in range(10)],
        per_keyword=cap * 2, survives_one_in=1,
    )

    assert len(pushed) == cap  # qualified cap reached
    assert len(searched) < 10, (
        f"searched {searched} — kept hunting for flagged rows this niche cannot produce"
    )


def test_flagged_still_gets_a_pass_when_qualified_is_already_full(monkeypatch):
    """Qualified filled earlier today, flagged budget still open: one round runs."""
    searched, _, _, _ = _run(
        monkeypatch, [f"kw{i}" for i in range(10)],
        per_keyword=20, survives_one_in=1,
        qualified_today=pipeline.DAILY_QUALIFIED_CAP,
    )

    assert 1 <= len(searched) <= 2, f"searched {searched}"


def test_runs_dry_without_looping_forever(monkeypatch):
    """Keywords exhausted before the budget fills: finish under budget, don't hang."""
    searched, pushed, _, cap_ok = _run(
        monkeypatch, ["kw0", "kw1"], per_keyword=10, survives_one_in=5,
    )

    assert len(searched) == 2          # each keyword searched exactly once
    assert len(pushed) == 4            # 20 candidates, 1 in 5 survives
    assert cap_ok is True


def test_already_pushed_candidates_are_not_re_enriched(monkeypatch):
    """A candidate examined in one batch must not come back in the next."""
    searched = []
    examined = []

    monkeypatch.setattr(pipeline, "run_discovery", _fake_discovery(searched, per_keyword=20))
    monkeypatch.setattr(pipeline, "push_record", lambda t, r: True)
    monkeypatch.setattr(pipeline, "count_added_today", lambda table, qualification=None: 0)

    def process_candidate(candidate, *a, **k):
        examined.append(candidate["channel_id"])
        return None, "below_view_minimum"

    monkeypatch.setattr(pipeline, "process_candidate", process_candidate)

    pipeline.run_niche(
        niche_name="Home Theater", table_name="tbl",
        keywords=[f"kw{i}" for i in range(4)], max_results_per_keyword=50, days_back=7,
        globally_tracked_ids=set(), external_handles={},
        blocklist=_NullBlocklist(),
        niche_config={"min_avg_views": 10_000, "min_channel_age_months": 12, "allowed_country_codes": ZONE_CORE},
        scraper=None,
    )

    assert len(examined) == len(set(examined)), "a candidate was enriched twice"


def test_partial_day_headroom_is_respected(monkeypatch):
    """A second run the same day tops up to the cap rather than doubling it."""
    cap = pipeline.DAILY_QUALIFIED_CAP
    already = cap - 5          # leave exactly 5 rows of headroom
    _, pushed, _, _ = _run(
        monkeypatch, [f"kw{i}" for i in range(10)],
        per_keyword=cap, survives_one_in=5, qualified_today=already,
    )

    assert len(pushed) == 5, f"expected the 5 remaining of {cap}, got {len(pushed)}"
