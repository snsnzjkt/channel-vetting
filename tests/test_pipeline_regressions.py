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
from scoring import BELOW_VIEW_MINIMUM


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
    }
    stats.update(overrides)
    return stats


def _stub_performance(**overrides):
    performance = {
        "avg_views": 5_000,
        "avg_engagement_rate": 1.0,
        "upload_dates": [],
        "content_language": "",
        "repeated_email": "",
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
            "min_channel_age_months": None,
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
            "min_channel_age_months": None,
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
        niche_config={"min_avg_views": 0, "min_channel_age_months": None},
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
        niche_config={"min_avg_views": 0, "min_channel_age_months": None},
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
    monkeypatch.setattr(main, "resolve_email", lambda stats, performance, scraper: "creator@blocked.example")
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "push_record", lambda t, r: pytest.fail("blocked candidate must never be pushed"))

    niche_config = {"min_avg_views": 0, "min_channel_age_months": None}
    candidate = {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []}

    result = main.push_until_full(
        [candidate],
        lambda c: main.process_candidate(c, {}, _EmailOnlyBlocklist(), niche_config, None),
        "tbl",
        qualified_headroom=5,
        flagged_headroom=5,
    )

    assert result == {"qualified": 0, "flagged": 0, "skipped": 1, "pushed_ids": set()}


# --- M13: resolve_email must be called WITH the scraper -------------------

def test_process_candidate_passes_scraper_to_resolve_email(monkeypatch):
    import main

    sentinel_scraper = object()
    received = {}

    def fake_resolve_email(stats, performance, scraper=None):
        received["scraper"] = scraper
        return ""

    monkeypatch.setattr(main, "get_channel_stats", lambda cid: _stub_stats())
    monkeypatch.setattr(main, "get_recent_video_performance", lambda cid, pl: _stub_performance())
    monkeypatch.setattr(main, "resolve_email", fake_resolve_email)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    candidate = {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []}
    niche_config = {"min_avg_views": 0, "min_channel_age_months": None}

    main.process_candidate(candidate, {}, _NullBlocklist(), niche_config, sentinel_scraper)

    # If the scraper argument were dropped, resolve_email's default
    # (scraper=None) would silently swallow this — the browser step goes
    # dead with no signal, which is exactly why this must be pinned.
    assert received.get("scraper") is sentinel_scraper


# --- Bonus: qualify()'s threshold arguments must not be swapped at the call site --

def test_process_candidate_does_not_swap_qualify_thresholds(monkeypatch):
    """
    min_avg_views=10000, min_channel_age_months=6; avg_views=5000 (below
    the view floor), channel_age_months=100 (well above the age floor).
    Correctly wired, this must fail on VIEWS (BELOW_VIEW_MINIMUM). If the
    two threshold arguments to qualify() were swapped at the call site,
    5000 would compare against a threshold of 6 (passes) and 100 would
    compare against a threshold of 10000 (fails) -> NEW_CHANNEL instead.
    """
    import main

    monkeypatch.setattr(main, "get_channel_stats", lambda cid: _stub_stats())
    monkeypatch.setattr(main, "get_recent_video_performance", lambda cid, pl: _stub_performance(avg_views=5000))
    monkeypatch.setattr(main, "channel_age_months", lambda published_at: 100)
    monkeypatch.setattr(main, "resolve_email", lambda stats, performance, scraper: "")
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    candidate = {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []}
    niche_config = {"min_avg_views": 10_000, "min_channel_age_months": 6}

    _record, qualification = main.process_candidate(candidate, {}, _NullBlocklist(), niche_config, None)

    assert qualification == BELOW_VIEW_MINIMUM


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

    def fake_run(niches, max_results_per_keyword, days_back):
        captured["qualified_cap"] = main.DAILY_QUALIFIED_CAP
        captured["flagged_cap"] = main.DAILY_FLAGGED_CAP

    monkeypatch.setattr(main, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["main.py", "--test", "--daily-cap", "2"])

    try:
        main.main()
        assert captured == {"qualified_cap": 2, "flagged_cap": 2}
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
            "min_channel_age_months": None,
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
            "min_channel_age_months": None,
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
            "min_channel_age_months": None,
        },
        "No Keywords": {
            "table_name": "tbl",
            # missing "keywords"
            "min_avg_views": 0,
            "min_channel_age_months": None,
        },
    }

    with pytest.raises(SystemExit):
        # Both niches are misconfigured, so no niche completes its cap
        # check and IMPORTANT 3's guard fires — proving the KeyError never
        # had a chance to happen first.
        main.run(niches, max_results_per_keyword=5, days_back=90)
