"""
Wiring for the influencers.club discovery source (the switch away from
search.list):

  - enrichment.get_channel_stats(handle=...) resolves a creator's real UC…
    id from the channels.list?forHandle response, so a discovery candidate
    (which arrives as an @handle) can enter the existing enrich pipeline;
  - main.process_candidate accepts a handle-only candidate and dedupes by the
    RESOLVED channel id (known_channel_ids), the only place a re-discovered
    niche-table channel is caught since discovery can't be pre-filtered by id;
  - main.run_niche drives discovery.discover() in place of the keyword loop
    when a discovery client is enabled and the niche carries filters,
    accumulating exclude_handles across rounds and stopping when dry, and
    falls back to search.list when discovery is unavailable.

No network: enrichment HTTP, Airtable, and process_candidate are all mocked.
"""
import pytest

import enrichment
import main


class _NullBlocklist:
    handles: set = set()

    def match(self, handle="", email="", name=""):
        return ""


class _Blocklist:
    """A DO NOT CONTACT blocklist stub carrying a handle index."""

    def __init__(self, handles):
        self.handles = set(handles)

    def match(self, handle="", email="", name=""):
        return "handle" if handle.lstrip("@").lower() in self.handles else ""


# --- get_channel_stats forHandle -------------------------------------------

class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


def _channel_payload(channel_id="UCreal"):
    return {"items": [{
        "id": channel_id,
        "snippet": {"title": "A Channel", "country": "US",
                    "publishedAt": "2020-01-01T00:00:00Z", "description": "",
                    "customUrl": "@achannel"},
        "statistics": {"subscriberCount": "1000", "videoCount": "50", "viewCount": "9999"},
        "contentDetails": {"relatedPlaylists": {"uploads": "PL1"}},
    }]}


def _patch_enrichment(monkeypatch, resp):
    seen = []

    def fake_get(url, params=None, timeout=None):
        seen.append(params or {})
        return resp

    monkeypatch.setattr(enrichment.HTTP, "get", fake_get)
    monkeypatch.setattr(enrichment.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(enrichment, "record_spend", lambda *a, **k: None)
    return seen


def test_get_channel_stats_by_handle_resolves_the_real_id(monkeypatch):
    seen = _patch_enrichment(monkeypatch, _Resp(200, _channel_payload("UCresolved")))

    stats = enrichment.get_channel_stats(handle="somecreator")

    assert stats["channel_id"] == "UCresolved"       # read from the response
    assert seen[0].get("forHandle") == "@somecreator"  # queried by handle
    assert "id" not in seen[0]


def test_get_channel_stats_by_id_still_reads_the_response_id(monkeypatch):
    seen = _patch_enrichment(monkeypatch, _Resp(200, _channel_payload("UC1")))

    stats = enrichment.get_channel_stats("UC1")

    assert stats["channel_id"] == "UC1"
    assert seen[0].get("id") == "UC1"
    assert "forHandle" not in seen[0]


def test_get_channel_stats_requires_exactly_one_key():
    with pytest.raises(ValueError):
        enrichment.get_channel_stats()                 # neither
    with pytest.raises(ValueError):
        enrichment.get_channel_stats("UC1", handle="x")  # both


# --- process_candidate: handle-first + resolved-id dedupe -------------------

def _niche():
    return {"min_avg_views": 10_000, "min_channel_age_months": 12,
            "discovery_filters": {"profile_language": ["en"]}}


def test_process_candidate_resolves_a_handle_only_candidate(monkeypatch):
    called = {}

    def fake_stats(channel_id=None, *, handle=None):
        called["channel_id"] = channel_id
        called["handle"] = handle
        return None  # short-circuit right after routing

    monkeypatch.setattr(main, "get_channel_stats", fake_stats)
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)

    record, reason = main.process_candidate(
        {"handle": "creatorx", "channel_title": "X"}, {}, _NullBlocklist(),
        _niche(), None,
    )
    assert record is None and reason == "unreachable"
    assert called == {"channel_id": None, "handle": "creatorx"}


def test_process_candidate_skips_a_channel_already_in_the_niche_table(monkeypatch):
    monkeypatch.setattr(
        main, "get_channel_stats",
        lambda channel_id=None, *, handle=None: {
            "channel_id": "UC1", "channel_title": "X", "handle": "h1",
        },
    )
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)

    record, reason = main.process_candidate(
        {"channel_id": "UC1", "channel_title": "X"}, {}, _NullBlocklist(),
        _niche(), None, known_channel_ids={"UC1"},
    )
    assert record is None and reason == "duplicate"


# --- run_niche: discovery mode ---------------------------------------------

def test_process_candidate_skips_a_renamed_channel_tracked_externally_by_name(monkeypatch):
    """The 'New Record Day' bug: already in Follow-up Outreach under the old
    handle @Newrecordday2013, now @newrecordday. The handle no longer matches
    the external index, but the channel NAME does — so it must be skipped, not
    re-added to Prospects."""
    monkeypatch.setattr(
        main, "get_channel_stats",
        lambda channel_id=None, *, handle=None: {
            "channel_id": "UCFnhdh4", "channel_title": "New Record Day",
            "handle": "newrecordday", "description": "",
        },
    )
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)

    external = main.ExternalIndex(
        handles={"newrecordday2013": "Follow-up Outreach"},   # stale handle
        names={"new record day": "Follow-up Outreach"},
    )
    record, reason = main.process_candidate(
        {"channel_id": "UCFnhdh4", "channel_title": "New Record Day"}, external,
        _NullBlocklist(), _niche(), None,
    )
    assert record is None and reason == "duplicate"


def test_process_candidate_drops_a_fake_usa_channel_by_its_description(monkeypatch):
    """Declares country US but the About says 'based in the Philippines' — the
    real location is outside the zones, so it's dropped before the
    performance-fetch quota is spent."""
    calls = {"perf": 0}
    monkeypatch.setattr(
        main, "get_channel_stats",
        lambda channel_id=None, *, handle=None: {
            "channel_id": "UC1", "channel_title": "Budget Home Theater",
            "handle": "budgetht", "country": "US",
            "description": "Home theater on a budget. Based in the Philippines.",
        },
    )
    monkeypatch.setattr(main, "get_recent_video_performance",
                        lambda *a, **k: calls.__setitem__("perf", calls["perf"] + 1))
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)

    record, reason = main.process_candidate(
        {"channel_id": "UC1", "channel_title": "Budget Home Theater"}, main.ExternalIndex(),
        _NullBlocklist(), _niche(), None,
    )
    assert record is None
    assert reason == main.DROP_OUTSIDE_SEARCH_ZONE
    assert calls["perf"] == 0, "a fake-USA channel must be dropped before the performance fetch"


class _FakeDiscovery:
    def __init__(self, batches, enabled=True):
        self._batches = list(batches)
        self.enabled = enabled
        self.credits_spent = 0.0
        self.calls = []

    def discover(self, *, filters, target, exclude_handles=(), source_label=""):
        self.calls.append({"target": target, "exclude": set(exclude_handles)})
        batch = self._batches.pop(0) if self._batches else []
        self.credits_spent += 0.01 * len(batch)
        return [{"handle": h, "channel_title": h, "matched_keywords": [source_label]} for h in batch]


def _survives_one_in(n):
    seen = {"n": 0}

    def process_candidate(candidate, *a, **k):
        seen["n"] += 1
        if seen["n"] % n:
            return None, "below_view_minimum"
        return {"Channel ID": candidate["handle"], "Qualification": "Qualified"}, "Qualified"

    return process_candidate


def _run_niche_discovery(monkeypatch, discovery, survives_one_in=1, blocklist=None,
                         external_handles=None):
    pushed = []
    monkeypatch.setattr(main, "process_candidate", _survives_one_in(survives_one_in))
    monkeypatch.setattr(main, "push_record", lambda t, r: pushed.append(r) or True)
    monkeypatch.setattr(main, "count_added_today", lambda table, qualification=None: 0)

    result = main.run_niche(
        "Home Theater", "tbl", ["kw"], 50, 7, set(), external_handles or {},
        blocklist or _NullBlocklist(),
        {"min_avg_views": 10_000, "min_channel_age_months": 12,
         "discovery_filters": {"profile_language": ["en"]}},
        None, None, discovery,
    )
    return pushed, result


def test_run_niche_fills_the_budget_from_discovery(monkeypatch):
    disc = _FakeDiscovery([[f"h{i}" for i in range(50)]])
    pushed, (discovered, processed, pushed_ids, cap_ok) = _run_niche_discovery(monkeypatch, disc)

    assert len(pushed) == main.DAILY_QUALIFIED_CAP == 30
    assert cap_ok is True
    assert len(disc.calls) == 1  # one round filled it


def test_run_niche_accumulates_exclude_handles_across_rounds(monkeypatch):
    # Low survival forces multiple rounds; the second round must exclude the
    # handles the first already examined, so the vendor never re-returns them.
    disc = _FakeDiscovery([[f"a{i}" for i in range(60)], [f"b{i}" for i in range(60)]])
    _run_niche_discovery(monkeypatch, disc, survives_one_in=5)

    assert len(disc.calls) >= 2
    first_batch = {f"a{i}" for i in range(60)}
    assert first_batch <= disc.calls[1]["exclude"], (
        "round 2 did not exclude the handles round 1 already spent on"
    )


# --- the 2026-08-13 credit-waste regression --------------------------------
# The vendor bills 0.01 per creator RETURNED and its minimum page is 50, so a
# page costs 0.5 credits however few the caller asked for. The old code trimmed
# discover()'s return to `target` (4, on a 3-row headroom), examined those 4,
# dropped the other 46 WITHOUT recording them, and so re-bought the same page
# every round: 32 rounds, 16.00 credits, one qualified row.


def _recording_process_candidate(examined, survives_one_in=999):
    def process_candidate(candidate, *a, **k):
        examined.append(candidate["handle"])
        if len(examined) % survives_one_in:
            return None, "below_view_minimum"
        return {"Channel ID": candidate["handle"], "Qualification": "Qualified"}, "Qualified"

    return process_candidate


def _run_with_caps(monkeypatch, discovery, examined, qualified_cap, flagged_cap,
                   survives_one_in=999):
    monkeypatch.setattr(main, "DAILY_QUALIFIED_CAP", qualified_cap)
    monkeypatch.setattr(main, "DAILY_FLAGGED_CAP", flagged_cap)
    monkeypatch.setattr(
        main, "process_candidate", _recording_process_candidate(examined, survives_one_in))
    monkeypatch.setattr(main, "push_record", lambda t, r: True)
    monkeypatch.setattr(main, "count_added_today", lambda table, qualification=None: 0)
    return main.run_niche(
        "Home Theater", "tbl", ["kw"], 50, 7, set(), {}, _NullBlocklist(),
        {"min_avg_views": 10_000, "min_channel_age_months": 12,
         "discovery_filters": {"profile_language": ["en"]}},
        None, None, discovery,
    )


def test_a_billed_page_is_fully_spent_before_another_is_bought(monkeypatch):
    """
    One page purchased, all 50 of its creators examined. The old code bought
    the page ~32 times over to examine 4 each time.
    """
    examined = []
    disc = _FakeDiscovery([[f"h{i}" for i in range(50)]])

    _run_with_caps(monkeypatch, disc, examined, qualified_cap=2, flagged_cap=1)

    # The purchase count is what the money bug was about. The trailing call is
    # the dry check that ends the loop, so at most two.
    assert len(disc.calls) <= 2, f"re-bought discovery {len(disc.calls)} times"
    # Every creator we paid for got looked at, and none twice.
    assert sorted(examined) == sorted(f"h{i}" for i in range(50))
    assert disc.credits_spent == pytest.approx(0.5)


def test_unexamined_but_billed_handles_are_still_excluded_next_round(monkeypatch):
    """
    The precise mechanism of the leak. A page of 50 against a 3-row headroom
    leaves ~46 creators billed but not yet examined. Those MUST still reach the
    next request's exclude_handles, or the vendor returns and re-bills them.
    """
    examined = []
    # Two pages so there is a second request to inspect.
    disc = _FakeDiscovery([[f"a{i}" for i in range(50)], [f"b{i}" for i in range(50)]])

    _run_with_caps(monkeypatch, disc, examined, qualified_cap=2, flagged_cap=1)

    assert len(disc.calls) >= 2, "expected a second request once the page was spent"
    first_page = {f"a{i}" for i in range(50)}
    assert first_page <= disc.calls[1]["exclude"], (
        "handles that were billed but not examined fell out of exclude_handles — "
        "the vendor will return and re-bill them"
    )


def test_only_the_headroom_is_examined_per_round(monkeypatch):
    """
    The other half of C1, and the reason returning all 50 is safe: a round
    examines at most `target` candidates, so buying a bigger page does not
    multiply YouTube enrichment quota (~3-13 units each). Here the qualified
    budget fills on the first two candidates, so the remaining ~48 stay
    backlogged and unexamined rather than being enriched and thrown away.
    """
    examined = []
    disc = _FakeDiscovery([[f"h{i}" for i in range(50)]])

    _run_with_caps(monkeypatch, disc, examined, qualified_cap=2, flagged_cap=0,
                   survives_one_in=1)

    assert len(examined) < 50, (
        f"examined {len(examined)} candidates for a 2-row budget — the backlog "
        "is not bounding enrichment"
    )


def test_the_discovery_subscriber_floor_tracks_each_niche_view_floor():
    """
    The subscriber floor sent to the vendor is DERIVED from the niche's own
    min_avg_views, not hardcoded. Every other qualification lever is per-niche,
    and these two have diverged before (Lifestyle Sofa's view floor was 2,000
    until the unification). An absolute floor would silently stop matching the
    arithmetic that justified it the next time a view floor moves.
    """
    for niche_name, config in main.NICHES.items():
        filters = config.get("discovery_filters")
        if filters is None:
            continue
        expected = int(config["min_avg_views"] * main.DISCOVERY_SUBSCRIBER_FLOOR_RATIO)
        assert filters["number_of_subscribers"] == {"min": expected}, (
            f"{niche_name}'s discovery subscriber floor drifted from its view floor"
        )


def test_a_niche_missing_min_avg_views_does_not_break_import():
    """
    wire_discovery_filters runs while `import main` is still executing, so a
    KeyError there kills the run before logging is configured, before the
    blocklist fetch, before any niche is attempted — strictly worse than the
    failure this project designed for, where run_niche() checks the same keys
    against REQUIRED_NICHE_KEYS and skips only the offending niche.

    Calls the REAL function (not a hand-copy of its body) so it still catches a
    regression if the wiring changes shape.
    """
    niches = {"Broken": {"discovery_filters": {"profile_language": ["en"]}}}

    main.wire_discovery_filters(niches)  # must not raise

    filters = niches["Broken"]["discovery_filters"]
    # The filter it COULD wire is still wired...
    assert filters["keywords_not_in_description"] == list(main.EXCLUDED_TOPIC_KEYWORDS)
    # ...and the one it couldn't is simply absent, rather than a crash or a
    # wrong default sent to the vendor.
    assert "number_of_subscribers" not in filters
    # The misconfiguration is still caught — later, and survivably.
    assert "min_avg_views" in main.REQUIRED_NICHE_KEYS


def test_a_search_only_niche_is_left_untouched():
    """A niche with no discovery_filters (search.list path) gains nothing."""
    niches = {"Keywords Only": {"min_avg_views": 10_000}}

    main.wire_discovery_filters(niches)

    assert niches == {"Keywords Only": {"min_avg_views": 10_000}}


def test_each_niche_gets_its_own_exclusion_list():
    """
    Per-niche list() copies, so a future per-niche edit can't mutate the other
    niche's filter or EXCLUDED_TOPIC_KEYWORDS itself in place.
    """
    niches = {
        "A": {"min_avg_views": 10_000, "discovery_filters": {}},
        "B": {"min_avg_views": 10_000, "discovery_filters": {}},
    }

    main.wire_discovery_filters(niches)

    a = niches["A"]["discovery_filters"]["keywords_not_in_description"]
    b = niches["B"]["discovery_filters"]["keywords_not_in_description"]
    assert a == b
    assert a is not b
    assert a is not main.EXCLUDED_TOPIC_KEYWORDS


def test_each_niche_targets_the_creator_gender_its_brief_asks_for():
    """
    Home Theater is a men's-product brief, Lifestyle Sofa a women's one, and the
    vendor filters CREATOR gender server-side. Pinned because it is config with
    no other test coverage: a wrong value here silently sources the wrong
    audience for a whole run, and nothing downstream would flag it.
    """
    assert main.NICHES["Home Theater"]["discovery_filters"]["gender"] == "male"
    assert main.NICHES["Lifestyle Sofa"]["discovery_filters"]["gender"] == "female"


def test_the_niche_filters_reach_the_vendor_payload(monkeypatch):
    """
    The wiring loop rewrites discovery_filters at import, so this asserts the
    end-to-end path: whatever NICHES declares is what discover() is handed, with
    gender, language and the derived subscriber floor all intact.
    """
    seen = {}

    class _CapturingDiscovery:
        enabled = True
        credits_spent = 0.0
        creators_billed = 0

        def discover(self, *, filters, target, exclude_handles=(), source_label=""):
            seen["filters"] = filters
            return []

    monkeypatch.setattr(main, "count_added_today", lambda table, qualification=None: 0)
    monkeypatch.setattr(main, "process_candidate", lambda *a, **k: (None, "skip"))

    niche_config = main.NICHES["Home Theater"]
    main.run_niche(
        "Home Theater", "tbl", ["kw"], 50, 7, set(), {}, _NullBlocklist(),
        niche_config, None, None, _CapturingDiscovery(),
    )

    sent = seen["filters"]
    assert sent["gender"] == "male"
    assert sent["profile_language"] == ["en"]
    assert sent["number_of_subscribers"] == {"min": 5000}
    assert "keywords_not_in_description" in sent


def test_the_quota_ceiling_stops_enrichment(monkeypatch):
    """
    can_afford_search() gates search.list only, and the discovery source
    REPLACES search.list — so before 2026-08-14 a discovery run consulted
    QUOTA_CEILING nowhere and enrichment spend was bounded by nothing but the
    daily row cap, which counts rows WRITTEN rather than candidates EXAMINED.
    process_candidate now refuses to start a candidate it cannot afford.
    """
    import quota_tracker

    monkeypatch.setattr(main, "can_afford_enrichment", lambda: False)
    # Would spend quota if reached; must not be.
    monkeypatch.setattr(
        main, "get_channel_stats",
        lambda *a, **k: pytest.fail("enrichment ran past an exhausted quota ceiling"))

    record, reason = main.process_candidate(
        {"channel_id": "UC1", "channel_title": "Anything", "matched_keywords": []},
        {}, _NullBlocklist(),
        {"min_avg_views": 10_000, "min_channel_age_months": None}, None,
    )

    assert record is None
    assert reason == main.DROP_QUOTA_EXHAUSTED
    # Distinct from the criteria-based drop reasons: this says "come back
    # tomorrow", not "this channel failed".
    assert reason not in {
        main.DROP_BELOW_VIEW_MINIMUM, main.DROP_DEAD_CHANNEL, main.DROP_NOT_ENGLISH,
    }
    assert quota_tracker.can_afford_enrichment  # the real gate still exists


class _NameBlocklist:
    """Matches on channel NAME, which is checkpoint 1's free pre-enrich key."""

    handles: set = set()

    def __init__(self, name):
        self._name = name

    def match(self, handle="", email="", name=""):
        return "name" if name == self._name else ""


def test_the_blocklist_is_checked_before_the_quota_gate(monkeypatch):
    """
    A blocklist match is free; a quota refusal must not mask it, or a run that
    hit its ceiling would report DO NOT CONTACT channels as merely deferred and
    a later run would re-consider them.
    """
    monkeypatch.setattr(main, "can_afford_enrichment", lambda: False)

    record, reason = main.process_candidate(
        {"channel_id": "UC1", "channel_title": "Blocked Co", "matched_keywords": []},
        {}, _NameBlocklist("Blocked Co"),
        {"min_avg_views": 10_000, "min_channel_age_months": None}, None,
    )

    assert record is None
    assert reason == "blocked"


def test_run_niche_stops_when_discovery_is_dry(monkeypatch):
    # Only one small batch, none survive -> the loop must terminate, not spin.
    disc = _FakeDiscovery([["x0", "x1", "x2"]])
    pushed, _ = _run_niche_discovery(monkeypatch, disc, survives_one_in=999)

    assert pushed == []
    assert len(disc.calls) <= 2  # discovered once, saw the empty follow-up, stopped


# --- run_niche: fallback to search.list ------------------------------------

def test_run_niche_falls_back_to_search_when_discovery_is_disabled(monkeypatch):
    searched = []

    def fake_run_discovery(keywords, max_results_per_keyword=50, days_back=90,
                           exclude_ids=None, target_fresh=None):
        searched.extend(keywords)
        return [{"channel_id": f"UC-{k}", "channel_title": k, "matched_keywords": [k]}
                for k in keywords]

    monkeypatch.setattr(main, "run_discovery", fake_run_discovery)
    monkeypatch.setattr(
        main, "process_candidate",
        lambda c, *a, **k: ({"Channel ID": c["channel_id"], "Qualification": "Qualified"}, "Qualified"),
    )
    monkeypatch.setattr(main, "push_record", lambda t, r: True)
    monkeypatch.setattr(main, "count_added_today", lambda table, qualification=None: 0)

    disabled = _FakeDiscovery([], enabled=False)
    main.run_niche(
        "Home Theater", "tbl", ["kw0", "kw1"], 50, 7, set(), {}, _NullBlocklist(),
        {"min_avg_views": 10_000, "min_channel_age_months": 12,
         "discovery_filters": {"profile_language": ["en"]}},
        None, None, disabled,
    )
    assert searched, "a disabled discovery client must fall through to search.list"
    assert disabled.calls == [], "a disabled discovery client must never be queried"


# --- DO NOT CONTACT is the priority exclusion ------------------------------

def test_do_not_contact_handles_are_sent_to_discovery(monkeypatch):
    """The suppression list must be excluded server-side so no credit is spent
    surfacing a creator that would be dropped by the checkpoints anyway."""
    disc = _FakeDiscovery([["fresh0", "fresh1"]])
    _run_niche_discovery(
        monkeypatch, disc,
        blocklist=_Blocklist({"blockedcreator", "agencyhandle"}),
        external_handles={"knownelsewhere": "Outreach"},
    )
    excl = disc.calls[0]["exclude"]
    assert {"blockedcreator", "agencyhandle"} <= excl, "DO NOT CONTACT handles must be excluded"
    assert "knownelsewhere" in excl, "external handles fill the remaining room"


def test_exclude_prioritises_do_not_contact_over_external_under_the_cap(monkeypatch):
    monkeypatch.setattr(main, "INFLUENCERS_MAX_EXCLUDE_HANDLES", 3)
    bl = _Blocklist({"dnc1", "dnc2", "dnc3"})
    external = {f"ext{i}": "T" for i in range(100)}

    got = main._discovery_exclude_handles(bl, external, seen_handles=set())

    # The cap is exactly filled by DO NOT CONTACT, so no external handle
    # displaces one — the suppression list is never dropped to make room.
    assert got == {"dnc1", "dnc2", "dnc3"}


def test_exclude_keeps_do_not_contact_and_seen_then_fills_with_external(monkeypatch):
    monkeypatch.setattr(main, "INFLUENCERS_MAX_EXCLUDE_HANDLES", 5)
    bl = _Blocklist({"dnc1", "dnc2"})
    external = {f"ext{i}": "T" for i in range(100)}

    got = main._discovery_exclude_handles(bl, external, seen_handles={"seen1"})

    assert {"dnc1", "dnc2", "seen1"} <= got   # must-keeps always present
    assert len(got) == 5                       # filled to the cap
    assert len(got & set(external)) == 2       # only the 2 remaining slots
