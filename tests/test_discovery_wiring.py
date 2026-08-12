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
