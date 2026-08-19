"""
Integration-level regression coverage for main.py's run() / run_niche() /
process_candidate() wiring.

tests/test_run_niche_caps.py (prescribed verbatim by the Task 8 brief)
only exercises push_until_full() in isolation. A mutation-testing pass
on the whole suite found ten survivors — code paths the unit tests never
actually exercised even though the corresponding brief requirement was
implemented correctly. This file pins those paths so a future change
that silently deletes a safety check (the blocklist abort, the email
checkpoint, the AirtableReadError skip, ...) fails the suite instead of
shipping green.

No network: every YouTube/Airtable/browser call is monkeypatched.
"""
import sys

import pytest

from airtable_client import AirtableReadError
from do_not_contact import BlocklistUnavailable
from search_zones import ZONE_CORE


class _NullBlocklist:
    """Never matches anything — the default stand-in when a test isn't
    exercising blocklist behaviour itself."""

    def match(self, handle="", email="", name=""):
        return ""


class _EmailOnlyBlocklist:
    """Matches only on email, so tests can isolate checkpoint 3 without
    checkpoints 1/2 (name, handle) firing first."""

    def match(self, handle="", email="", name=""):
        if email:
            return f"email {email}"
        return ""


def _stub_stats(**overrides):
    stats = {
        "channel_id": "UC1",
        "channel_title": "Chan",
        "handle": "chan",
        "published_at": "",
        "subscriber_count": 10_000,
        "uploads_playlist_id": "PL1",
        "business_email": "",
        # Both added by the 2026-08 criteria change and both hard gates —
        # a stub missing them would be discarded before reaching the
        # behaviour these tests are actually about.
        "video_count": 100,
        "country": "US",
    }
    stats.update(overrides)
    return stats


def _stub_performance(**overrides):
    performance = {
        "avg_views": 5_000,
        "avg_engagement_rate": 1.0,
        "upload_dates": [],
        # "en", not "": an unset language is a hard DROP (main.is_english),
        # so a stub without one never reaches the behaviour under test.
        "content_language": "en",
        "repeated_email": "",
        # Enough confirmed non-Shorts uploads to clear MIN_LONGFORM_VIDEO_COUNT
        # from the newest-50 window alone, so no test pages for more.
        "longform_count": 50,
        "duration_sample_size": 50,
        "next_page_token": "",
    }
    performance.update(overrides)
    return performance


# --- M12: an unavailable blocklist must abort the whole run --------------

def test_run_aborts_when_blocklist_unavailable(monkeypatch):
    """run() must raise SystemExit and must not have started any niche
    work — not even the base-wide dedupe fetch — when the blocklist
    can't be established."""
    import main

    def fake_fetch_blocklist():
        raise BlocklistUnavailable("DO NOT CONTACT fetch failed")

    calls = []
    monkeypatch.setattr(main, "fetch_blocklist", fake_fetch_blocklist)
    monkeypatch.setattr(main, "get_existing_channel_ids", lambda t: calls.append(t) or set())

    niches = {
        "Test Niche": {
            "table_name": "tbl",
            "keywords": ["kw"],
            "min_avg_views": 0,
            "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE,
        }
    }

    with pytest.raises(SystemExit):
        main.run(niches, max_results_per_keyword=5, days_back=90)

    assert calls == []


# --- IMPORTANT 2: a partial existing-channel-ID set must abort the run, --
# --- not silently proceed and let already-tracked channels look "fresh" --

def test_run_aborts_when_get_existing_channel_ids_fails(monkeypatch):
    """
    A mid-pagination Airtable failure (e.g. a 429 on page 7 of 14) must
    not let run() proceed with a partial dedupe set: already-tracked
    channels would look fresh, get re-enriched, and get re-pushed via
    push_record's PATCH path — reverting a reviewer's Status and erasing
    their Notes. get_existing_channel_ids() raises AirtableReadError for
    exactly this reason; run() must catch it and abort before doing
    anything else (fetch_external_handles, any niche work).
    """
    import main

    monkeypatch.setattr(main, "fetch_blocklist", lambda: _NullBlocklist())

    def fake_get_ids(table_name):
        raise AirtableReadError("429 on page 7 of 14")

    monkeypatch.setattr(main, "get_existing_channel_ids", fake_get_ids)
    monkeypatch.setattr(
        main, "fetch_external_handles",
        lambda: pytest.fail("must abort before fetching external handles"),
    )

    niches = {
        "Test Niche": {
            "table_name": "tbl",
            "keywords": ["kw"],
            "min_avg_views": 0,
            "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE,
        }
    }

    with pytest.raises(SystemExit):
        main.run(niches, max_results_per_keyword=5, days_back=90)


# --- M10: an unreadable daily count must skip the niche, not grant a full budget --

def test_run_niche_skips_on_airtable_read_error(monkeypatch):
    import main

    def fake_count(table, qualification=None):
        raise AirtableReadError("read failed")

    discovery_calls = []
    monkeypatch.setattr(main, "count_added_today", fake_count)
    monkeypatch.setattr(main, "run_discovery", lambda *a, **k: discovery_calls.append(1) or [])

    result = main.run_niche(
        "Test", "tbl", ["kw"], 5, 90,
        globally_tracked_ids=set(),
        external_handles={},
        blocklist=_NullBlocklist(),
        niche_config={"min_avg_views": 0, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE},
        scraper=None,
    )

    assert result == (0, 0, set(), False)  # cap_check_completed=False: could not read, not "at cap"
    assert discovery_calls == []  # no quota spent on a niche we can't safely cap


# --- M11: zero headroom must spend zero quota -----------------------------

def test_run_niche_skips_run_discovery_when_already_at_cap(monkeypatch):
    import main

    def fake_count(table, qualification=None):
        if qualification == main.QUALIFIED:
            return main.DAILY_QUALIFIED_CAP
        return main.DAILY_QUALIFIED_CAP + main.DAILY_FLAGGED_CAP

    discovery_calls = []
    monkeypatch.setattr(main, "count_added_today", fake_count)
    monkeypatch.setattr(main, "run_discovery", lambda *a, **k: discovery_calls.append(1) or [])

    result = main.run_niche(
        "Test", "tbl", ["kw"], 5, 90,
        globally_tracked_ids=set(),
        external_handles={},
        blocklist=_NullBlocklist(),
        niche_config={"min_avg_views": 0, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE},
        scraper=None,
    )

    assert result == (0, 0, set(), True)  # cap_check_completed=True: legitimately at cap, a real no-op
    assert discovery_calls == []  # no 100-unit search.list calls for zero headroom


# --- IMPORTANT 2: a misconfigured niche must skip, not crash the run -----

def test_run_niche_skips_when_config_missing_thresholds(monkeypatch):
    import main

    discovery_calls = []
    monkeypatch.setattr(main, "run_discovery", lambda *a, **k: discovery_calls.append(1) or [])
    monkeypatch.setattr(
        main, "count_added_today",
        lambda *a, **k: pytest.fail("should not read Airtable counts for a misconfigured niche"),
    )

    result = main.run_niche(
        "Bad Niche", "tbl", ["kw"], 5, 90,
        globally_tracked_ids=set(),
        external_handles={},
        blocklist=_NullBlocklist(),
        niche_config={"min_avg_views": 1000},  # missing min_channel_age_months
        scraper=None,
    )

    assert result == (0, 0, set(), False)  # cap_check_completed=False: config never got that far
    assert discovery_calls == []


# --- M9: blocklist checkpoint 3 (resolved email) must still run ----------

def test_process_candidate_blocked_by_email_checkpoint(monkeypatch):
    import main

    monkeypatch.setattr(main, "get_channel_stats", lambda cid: _stub_stats())
    monkeypatch.setattr(main, "get_recent_video_performance", lambda cid, pl: _stub_performance())
    # process_candidate resolves the email via resolve_email_with_source now;
    # the None flag keeps the no-social drop from pre-empting the blocklist
    # checkpoint this test is exercising.
    monkeypatch.setattr(
        main, "resolve_email_with_source",
        lambda *a, **k: ("creator@blocked.example", "test-source", None),
    )
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "push_record", lambda t, r: pytest.fail("blocked candidate must never be pushed"))

    niche_config = {"min_avg_views": 0, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE}
    candidate = {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []}

    result = main.push_until_full(
        [candidate],
        lambda c: main.process_candidate(c, {}, _EmailOnlyBlocklist(), niche_config, None),
        "tbl",
        qualified_headroom=5,
        flagged_headroom=5,
    )

    assert result == {"qualified": 0, "flagged": 0, "skipped": 1, "pushed_ids": set()}


# --- M13: the email chain must be called WITH the scraper -----------------

def test_process_candidate_passes_scraper_to_resolve_email(monkeypatch):
    import main

    sentinel_scraper = object()
    received = {}

    def fake_resolve_email(stats, performance, scraper=None, enricher=None):
        received["scraper"] = scraper
        return "", "", None

    monkeypatch.setattr(main, "get_channel_stats", lambda cid: _stub_stats())
    monkeypatch.setattr(main, "get_recent_video_performance", lambda cid, pl: _stub_performance())
    # process_candidate calls resolve_email_with_source (it needs the
    # link-list-presence flag for the no-social drop), not resolve_email.
    monkeypatch.setattr(main, "resolve_email_with_source", fake_resolve_email)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    candidate = {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []}
    niche_config = {"min_avg_views": 0, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE}

    main.process_candidate(candidate, {}, _NullBlocklist(), niche_config, sentinel_scraper)

    # If the scraper argument were dropped, the chain's default (scraper=None)
    # would silently swallow this — the browser step goes dead with no signal,
    # which is exactly why this must be pinned.
    assert received.get("scraper") is sentinel_scraper


# --- no-social drop: an empty external link list is discarded --------------

def test_process_candidate_drops_a_channel_with_no_external_links(monkeypatch):
    """
    A channel whose About link list was read and came back EMPTY (no website,
    no social profile) AND that yielded no contact email has no outreach
    surface beyond YouTube — process_candidate must drop it, not push it.
    """
    import main

    monkeypatch.setattr(main, "get_channel_stats", lambda cid: _stub_stats())
    monkeypatch.setattr(main, "get_recent_video_performance", lambda cid, pl: _stub_performance())
    # has_external_links=False is the positive "no presence" signal.
    monkeypatch.setattr(
        main, "resolve_email_with_source", lambda *a, **k: ("", "", False),
    )
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    niche_config = {"min_avg_views": 0, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE}
    candidate = {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []}

    record, reason = main.process_candidate(candidate, {}, _NullBlocklist(), niche_config, None)
    assert record is None
    assert reason == main.DROP_NO_SOCIAL


def test_process_candidate_keeps_a_channel_when_link_presence_is_unknown(monkeypatch):
    """
    None (the link list was never read — browser off, or an address was found
    at an earlier step) must NOT drop: absent data never disqualifies, the
    same rule the zone check follows.
    """
    import main

    monkeypatch.setattr(main, "get_channel_stats", lambda cid: _stub_stats())
    monkeypatch.setattr(main, "get_recent_video_performance", lambda cid, pl: _stub_performance())
    monkeypatch.setattr(
        main, "resolve_email_with_source", lambda *a, **k: ("", "", None),
    )
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    niche_config = {"min_avg_views": 0, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE}
    candidate = {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []}

    record, reason = main.process_candidate(candidate, {}, _NullBlocklist(), niche_config, None)
    assert record is not None, "unknown link presence must be kept, not dropped"


# --- Bonus: the two niche thresholds must not be crossed at the call site --

def _run_process_candidate(monkeypatch, niche_config, *, avg_views, age, **stat_overrides):
    import main

    monkeypatch.setattr(main, "get_channel_stats", lambda cid: _stub_stats(**stat_overrides))
    monkeypatch.setattr(
        main, "get_recent_video_performance",
        lambda cid, pl: _stub_performance(avg_views=avg_views),
    )
    monkeypatch.setattr(main, "channel_age_months", lambda published_at: age)
    monkeypatch.setattr(main, "resolve_email_with_source", lambda *a, **k: ("", "", None))
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    candidate = {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []}
    return main.process_candidate(candidate, {}, _NullBlocklist(), niche_config, None)


def test_process_candidate_does_not_feed_the_view_floor_to_qualify(monkeypatch):
    """
    min_avg_views=10000, min_channel_age_months=6; avg_views=50000 (clears
    the view gate), channel_age_months=100 (well above the age floor).
    Correctly wired this is QUALIFIED. If min_avg_views were passed to
    qualify() where the age threshold belongs, 100 months would compare
    against a threshold of 10000 and every channel alive would come back
    NEW_CHANNEL.
    """
    from scoring import QUALIFIED

    niche_config = {"min_avg_views": 10_000, "min_channel_age_months": 6, "allowed_country_codes": ZONE_CORE}
    _record, qualification = _run_process_candidate(
        monkeypatch, niche_config, avg_views=50_000, age=100,
    )

    assert qualification == QUALIFIED


def test_process_candidate_still_flags_a_young_channel(monkeypatch):
    """The age gate is the one criterion that still produces a row."""
    from scoring import NEW_CHANNEL

    niche_config = {"min_avg_views": 10_000, "min_channel_age_months": 12, "allowed_country_codes": ZONE_CORE}
    record, qualification = _run_process_candidate(
        monkeypatch, niche_config, avg_views=50_000, age=3,
    )

    assert qualification == NEW_CHANNEL
    assert record is not None  # flagged for review, not discarded


# --- 2026-08: the three new hard gates discard instead of flagging --------

def test_process_candidate_drops_a_channel_below_the_view_floor(monkeypatch):
    """
    This used to be written as a "Below View Minimum" row. It must now
    produce NO record at all — that change is the whole point of retiring
    the value, so a returned record here means the flag path came back.
    """
    import main

    niche_config = {"min_avg_views": 10_000, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE}
    record, reason = _run_process_candidate(
        monkeypatch, niche_config, avg_views=5_000, age=100,
    )

    assert record is None
    assert reason == main.DROP_BELOW_VIEW_MINIMUM


def test_process_candidate_drops_a_channel_with_too_few_videos(monkeypatch):
    import main

    niche_config = {"min_avg_views": 10_000, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE}
    record, reason = _run_process_candidate(
        monkeypatch, niche_config, avg_views=50_000, age=100, video_count=12,
    )

    assert record is None
    assert reason == main.DROP_TOO_FEW_VIDEOS


def test_process_candidate_drops_a_channel_outside_the_search_zones(monkeypatch):
    import main

    niche_config = {"min_avg_views": 10_000, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE}
    record, reason = _run_process_candidate(
        monkeypatch, niche_config, avg_views=50_000, age=100, country="IN",
    )

    assert record is None
    assert reason == main.DROP_OUTSIDE_SEARCH_ZONE


def test_process_candidate_drops_a_channel_with_no_declared_country(monkeypatch):
    """
    2026-08-20 INVERSION, and the headline one. This test previously asserted
    the OPPOSITE — that an unknown country is absent data and the channel is
    kept for a human, the same rule as an unknown channel age.

    The instruction that reversed it was explicit: "don't include channels
    unless they have a specific location listed on YouTube". The old docstring
    also justified itself with "most channels never set snippet.country",
    which measurement did not support: over the 144 rows already tracked, only
    8 (5.6%) leave it blank.

    So a blank country is now evidence, not the absence of it. This and the
    English-language gate are the two places the project deliberately breaks
    its own "absent data never disqualifies" rule — do not "restore
    consistency" here without reading search_zones' docstring first.
    """
    import main

    niche_config = {"min_avg_views": 10_000, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE}
    record, reason = _run_process_candidate(
        monkeypatch, niche_config, avg_views=50_000, age=100, country="Unknown",
    )

    assert record is None
    assert reason == main.DROP_NO_DECLARED_COUNTRY


def test_the_language_region_subtag_is_no_longer_a_location(monkeypatch):
    """
    2026-08-20 INVERSION. `resolve_country` used to fall back to the content
    language's region subtag when snippet.country was blank, so an `en-IN`
    channel resolved to IN and an `en-US` one resolved to US.

    That fallback is deleted, and this test pins the direction it was deleted
    in: a channel with NO declared country is dropped as
    `no_declared_country`, and specifically NOT as `outside_search_zone` —
    which is what the old code would have said here, having read IN out of the
    tag. The reasons are distinct so a run summary can tell "we looked and
    they're out of zone" from "they told us nothing".

    Why it went: the tag describes the AUDIENCE. Measured on the live tables,
    `Lý Thiên An` and `Her 86m2` are both Vietnamese creators tagging `en-US`,
    and both were placed in zone by this step.
    """
    import main

    monkeypatch.setattr(main, "get_channel_stats", lambda cid: _stub_stats(country="Unknown"))
    monkeypatch.setattr(
        main, "get_recent_video_performance",
        lambda cid, pl: _stub_performance(avg_views=50_000, content_language="en-IN"),
    )
    monkeypatch.setattr(main, "channel_age_months", lambda published_at: 100)
    monkeypatch.setattr(main, "resolve_email_with_source", lambda *a, **k: ("", "", None))
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    candidate = {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []}
    niche_config = {"min_avg_views": 10_000, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE}

    record, reason = main.process_candidate(
        candidate, {}, _NullBlocklist(), niche_config, None,
    )

    assert record is None
    assert reason == main.DROP_NO_DECLARED_COUNTRY


def test_an_in_zone_language_tag_does_not_rescue_a_blank_country(monkeypatch):
    """
    The half of the inversion above that actually costs rows, and the half a
    well-meaning "fix" would restore: `en-US` on a channel that declares no
    country used to resolve to US and be KEPT. It is now dropped.

    This is the direction the instruction asked for — "don't include channels
    unless they have a specific location listed on YouTube" — and an `en-US`
    tag is not a location listed on YouTube.
    """
    import main

    monkeypatch.setattr(main, "get_channel_stats", lambda cid: _stub_stats(country=""))
    monkeypatch.setattr(
        main, "get_recent_video_performance",
        lambda cid, pl: _stub_performance(avg_views=50_000, content_language="en-US"),
    )
    monkeypatch.setattr(main, "channel_age_months", lambda published_at: 100)
    monkeypatch.setattr(main, "resolve_email_with_source", lambda *a, **k: ("", "", None))
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    candidate = {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []}
    niche_config = {"min_avg_views": 10_000, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE}

    record, reason = main.process_candidate(
        candidate, {}, _NullBlocklist(), niche_config, None,
    )

    assert record is None
    assert reason == main.DROP_NO_DECLARED_COUNTRY


def test_the_declared_country_wins_over_the_language_tag(monkeypatch):
    """
    Misscreative, live in the Home Theater table: `en-US` content, but
    snippet.country is IN. Four of the 29 live channels that declare a
    country contradict their language tag this way, which is why the tag
    is a fallback and never an override.
    """
    import main

    monkeypatch.setattr(main, "get_channel_stats", lambda cid: _stub_stats(country="IN"))
    monkeypatch.setattr(
        main, "get_recent_video_performance",
        lambda cid, pl: _stub_performance(avg_views=50_000, content_language="en-US"),
    )
    monkeypatch.setattr(main, "channel_age_months", lambda published_at: 100)
    monkeypatch.setattr(main, "resolve_email_with_source", lambda *a, **k: ("", "", None))
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    candidate = {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []}
    niche_config = {"min_avg_views": 10_000, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE}

    record, reason = main.process_candidate(
        candidate, {}, _NullBlocklist(), niche_config, None,
    )

    assert record is None
    assert reason == main.DROP_OUTSIDE_SEARCH_ZONE


def test_a_bare_language_no_longer_leaves_the_channel_admitted(monkeypatch):
    """
    2026-08-20 INVERSION of "an unknown country is absent data, so keep it".

    `en` still says nothing about where the creator is — that part is
    unchanged, and bare languages are still never mapped to countries. What
    changed is the CONSEQUENCE: a channel this pipeline cannot place is now
    discarded rather than written for a human to judge.

    Measured cost before choosing it: 8 of the 144 rows already tracked
    (5.6%). Measured reason: the reviewer found the rows that could not be
    placed were overwhelmingly outside the zone.
    """
    import main

    niche_config = {"min_avg_views": 10_000, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE}
    record, reason = _run_process_candidate(
        monkeypatch, niche_config, avg_views=50_000, age=100, country="Unknown",
    )

    assert record is None
    assert reason == main.DROP_NO_DECLARED_COUNTRY


def test_the_zone_gate_does_not_take_a_scraper():
    """
    The About panel's country is the same field as snippet.country and
    recovered 0 of the 5 live channels without one. A scraper argument
    appearing on the zone gate means that page load came back.

    Also pins that the gate reads only FREE inputs — title, description and
    the declared country, all already on the channels.list response, with no
    `performance` argument. That is what lets it run BEFORE
    get_recent_video_performance and save ~3 quota units per out-of-zone
    candidate; a `performance` parameter reappearing here means the gate has
    been pushed back below the paid fetch.
    """
    import inspect

    import main

    assert list(inspect.signature(main.location_drop_reason).parameters) == [
        "channel_title", "description", "declared_country", "allowed_codes",
    ]


# --- M2: a failed push must not land in pushed_ids ------------------------

def test_failed_push_not_in_pushed_ids(monkeypatch):
    import main

    monkeypatch.setattr(main, "push_record", lambda t, r: False)

    result = main.push_until_full(
        candidates=[{"channel_id": "UC1"}],
        build_record=lambda c: ({"Channel ID": c["channel_id"], "Qualification": "Qualified"}, "Qualified"),
        table_name="tbl",
        qualified_headroom=5,
        flagged_headroom=0,
    )

    assert result["pushed_ids"] == set()
    assert result["qualified"] == 0


# --- M3: a (None, reason) result must count as skipped, never as flagged --

def test_none_record_counts_as_skipped_not_flagged(monkeypatch):
    import main

    monkeypatch.setattr(main, "push_record", lambda t, r: pytest.fail("a None record must never be pushed"))

    result = main.push_until_full(
        candidates=[{"channel_id": "UC1"}],
        build_record=lambda c: (None, "unreachable"),
        table_name="tbl",
        qualified_headroom=5,
        flagged_headroom=5,
    )

    assert result == {"qualified": 0, "flagged": 0, "skipped": 1, "pushed_ids": set()}


# --- MINOR 3: --daily-cap must cap the flagged budget too, not just qualified --

def test_daily_cap_flag_caps_both_budgets(monkeypatch):
    """
    --daily-cap's stated purpose is cheap, safe testing of the capping
    path against production Airtable. Capping only DAILY_QUALIFIED_CAP
    left DAILY_FLAGGED_CAP at its full 10/day size, so `--daily-cap 2`
    could still write up to 10 flagged records per niche.
    """
    import main

    original_qualified = main.DAILY_QUALIFIED_CAP
    original_flagged = main.DAILY_FLAGGED_CAP
    captured = {}

    # **kwargs rather than a fixed signature: this stub stands in for run(),
    # and pinning its parameter list here means every new run() argument breaks
    # this test for a reason unrelated to what it checks (which is the caps).
    def fake_run(niches, max_results_per_keyword, days_back, **kwargs):
        captured["qualified_cap"] = main.DAILY_QUALIFIED_CAP
        captured["flagged_cap"] = main.DAILY_FLAGGED_CAP
        captured["discovery_credits"] = kwargs.get("max_discovery_credits")

    monkeypatch.setattr(main, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["main.py", "--test", "--daily-cap", "2"])

    try:
        main.main()
        # The discovery ceiling is asserted alongside the caps because the row
        # caps do NOT bound discovery spend — a --test run has to be handed its
        # own credit ceiling explicitly or it buys a 50-creator page per round.
        assert captured == {
            "qualified_cap": 2,
            "flagged_cap": 2,
            "discovery_credits": main.INFLUENCERS_TEST_DISCOVERY_CREDITS,
        }
    finally:
        # main() mutates these module globals directly (not via
        # monkeypatch), so they must be restored by hand.
        main.DAILY_QUALIFIED_CAP = original_qualified
        main.DAILY_FLAGGED_CAP = original_flagged


# --- IMPORTANT 3: every niche skipped for a non-cap reason must exit non-zero --

def _run_common_stubs(monkeypatch, main):
    monkeypatch.setattr(main, "fetch_blocklist", lambda: _NullBlocklist())
    monkeypatch.setattr(main, "get_existing_channel_ids", lambda t: set())
    monkeypatch.setattr(main, "fetch_external_handles", lambda: {})


def test_run_raises_when_every_niche_skips_for_non_cap_reason(monkeypatch):
    """
    An expired Airtable token (AirtableReadError from count_added_today)
    would previously make run_niche() return (0, 0, set()) for every
    niche, and run() had no notion of "nothing actually happened" — it
    would print a summary of all zeroes and exit 0, making a daily
    GitHub Actions job report green forever while doing nothing. run()
    must now raise SystemExit(1) when NO niche completed its cap check.
    """
    import main

    _run_common_stubs(monkeypatch, main)
    monkeypatch.setattr(
        main, "count_added_today",
        lambda *a, **k: (_ for _ in ()).throw(AirtableReadError("token expired")),
    )
    monkeypatch.setattr(main, "run_discovery", lambda *a, **k: pytest.fail("must not discover"))

    niches = {
        "Test Niche": {
            "table_name": "tbl",
            "keywords": ["kw"],
            "min_avg_views": 0,
            "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE,
        }
    }

    with pytest.raises(SystemExit):
        main.run(niches, max_results_per_keyword=5, days_back=90)


def test_run_raises_when_every_niche_is_missing_required_config(monkeypatch):
    """Same failure mode as above, but every NICHES entry is missing a
    required key (M3) rather than failing an Airtable read — must also
    exit non-zero rather than silently doing nothing."""
    import main

    _run_common_stubs(monkeypatch, main)
    monkeypatch.setattr(
        main, "count_added_today",
        lambda *a, **k: pytest.fail("should never reach a cap check for a misconfigured niche"),
    )

    niches = {
        "Bad Niche": {
            "table_name": "tbl",
            "keywords": ["kw"],
            "min_avg_views": 1000,
            # missing min_channel_age_months -> run() must skip it before
            # ever calling run_niche(), per the REQUIRED_NICHE_KEYS guard.
        }
    }

    with pytest.raises(SystemExit):
        main.run(niches, max_results_per_keyword=5, days_back=90)


def test_run_does_not_raise_when_a_niche_is_legitimately_at_cap(monkeypatch):
    """The counterpart to the tests above: a niche that's genuinely full
    for the day completed its cap check successfully, so this is a real
    no-op, not a failure — run() must NOT raise."""
    import main

    _run_common_stubs(monkeypatch, main)

    def fake_count(table, qualification=None):
        if qualification == main.QUALIFIED:
            return main.DAILY_QUALIFIED_CAP
        return main.DAILY_QUALIFIED_CAP + main.DAILY_FLAGGED_CAP

    monkeypatch.setattr(main, "count_added_today", fake_count)
    monkeypatch.setattr(main, "run_discovery", lambda *a, **k: pytest.fail("already at cap — must not discover"))

    niches = {
        "Test Niche": {
            "table_name": "tbl",
            "keywords": ["kw"],
            "min_avg_views": 0,
            "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE,
        }
    }

    main.run(niches, max_results_per_keyword=5, days_back=90)  # must not raise


# --- M3 (minor): a NICHES entry missing table_name/keywords must not KeyError --

def test_run_skips_niche_missing_table_name_or_keywords_key(monkeypatch):
    """
    run() used to index niche_config["table_name"] / niche_config["keywords"]
    directly, both in the base-wide dedupe loop and in the run_niche() call
    — a KeyError there would kill the entire run, not just the bad niche.
    A niche missing either key must be skipped instead, same as the
    existing min_avg_views/min_channel_age_months guard inside run_niche().
    """
    import main

    _run_common_stubs(monkeypatch, main)
    monkeypatch.setattr(main, "run_niche", lambda *a, **k: pytest.fail("must not call run_niche for a bad config"))

    niches = {
        "No Table": {
            # missing "table_name"
            "keywords": ["kw"],
            "min_avg_views": 0,
            "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE,
        },
        "No Keywords": {
            "table_name": "tbl",
            # missing "keywords"
            "min_avg_views": 0,
            "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE,
        },
    }

    with pytest.raises(SystemExit):
        # Both niches are misconfigured, so no niche completes its cap
        # check and IMPORTANT 3's guard fires — proving the KeyError never
        # had a chance to happen first.
        main.run(niches, max_results_per_keyword=5, days_back=90)
