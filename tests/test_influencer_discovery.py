"""
influencer_discovery.InfluencerDiscovery must:

  - turn the discovery endpoint's {accounts:[{user_id, profile:{username,
    full_name, followers, engagement_percent}}]} into pipeline candidate
    dicts keyed on a normalized @handle,
  - paginate until the caller's target is met and stop when supply or the
    credit ceiling runs out,
  - pass exclude_handles server-side (normalized, deduped, capped at 10k),
  - never raise: a non-200, a transport error, or a malformed body returns
    whatever was gathered so far,
  - be inert when no API key is configured.

All HTTP is mocked on `influencer_discovery.HTTP` — the shared INFLUENCERS
session — never on the real network (conftest hard-fails a real request).
"""
import logging

import influencer_discovery
from influencer_discovery import InfluencerDiscovery, null_discovery


class _Resp:
    def __init__(self, status_code, payload=None, text="body"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        if self._payload is _BAD_JSON:
            raise ValueError("no json")
        return self._payload


_BAD_JSON = object()


def _account(username, full_name="Name", followers=50000, engagement=2.5, user_id="u1"):
    return {
        "user_id": user_id,
        "profile": {
            "username": username,
            "full_name": full_name,
            "followers": followers,
            "engagement_percent": engagement,
        },
    }


def _resp(accounts, total=None, credits_cost=0.5, credits_left=None, status=200):
    payload = {"accounts": accounts, "total": total if total is not None else len(accounts)}
    if credits_cost is not None:
        payload["credits_cost"] = credits_cost
    if credits_left is not None:
        payload["credits_left"] = credits_left
    return _Resp(status, payload)


def _client(**kwargs):
    # No real sleeping between pages in tests.
    kwargs.setdefault("sleep", lambda _s: None)
    return InfluencerDiscovery(**kwargs)


# --- shaping accounts into candidates ---------------------------------------

def test_accounts_become_candidates(monkeypatch):
    resp = _resp([_account("@MrBeast", full_name="MrBeast", followers=512000000, engagement=2.2)])
    monkeypatch.setattr(influencer_discovery.HTTP, "post", lambda *a, **k: resp)

    got = _client().discover(filters={"profile_language": ["en"]}, target=5)

    assert got == [{
        "handle": "mrbeast",  # normalized: lowercased, '@' stripped
        "channel_title": "MrBeast",
        "influencers_user_id": "u1",
        "subscriber_count": 512000000,
        "engagement_percent": 2.2,
        "matched_keywords": ["influencers.club discovery"],
    }]


def test_accounts_without_a_handle_are_skipped(monkeypatch):
    resp = _resp([
        _account("", full_name="No Handle"),          # empty username
        _account("youtube.com/c/LegacyName"),          # /c/ url -> no @handle
        _account("@keep"),
    ])
    monkeypatch.setattr(influencer_discovery.HTTP, "post", lambda *a, **k: resp)

    got = _client().discover(filters={}, target=10)
    assert [c["handle"] for c in got] == ["keep"]


def test_same_handle_across_pages_is_deduped(monkeypatch):
    pages = [
        _resp([_account("@a"), _account("@b")], total=3),        # full page
        _resp([_account("@b"), _account("@c")], total=3),        # @b repeats; total reached
    ]
    # Force pagination: make the first page look "full" by patching PAGE_LIMIT.
    monkeypatch.setattr(influencer_discovery, "PAGE_LIMIT", 2)
    monkeypatch.setattr(influencer_discovery.HTTP, "post", lambda *a, **k: pages.pop(0))

    got = _client().discover(filters={}, target=10)
    assert sorted(c["handle"] for c in got) == ["a", "b", "c"]


# --- pagination + stopping conditions ---------------------------------------

def test_paginates_until_target_then_stops(monkeypatch):
    monkeypatch.setattr(influencer_discovery, "PAGE_LIMIT", 2)
    calls = {"n": 0}
    pages = [
        _resp([_account("@a"), _account("@b")], total=100),
        _resp([_account("@c"), _account("@d")], total=100),
        _resp([_account("@e"), _account("@f")], total=100),
    ]

    def fake_post(*a, **k):
        calls["n"] += 1
        return pages.pop(0)

    monkeypatch.setattr(influencer_discovery.HTTP, "post", fake_post)

    got = _client().discover(filters={}, target=3)
    assert len(got) == 3            # trimmed to the target
    assert calls["n"] == 2          # stopped after the second page met it


def test_short_page_ends_pagination(monkeypatch):
    monkeypatch.setattr(influencer_discovery, "PAGE_LIMIT", 50)
    # One page of 2 (< PAGE_LIMIT) means supply is exhausted; don't ask again.
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _resp([_account("@a"), _account("@b")], total=1000)

    monkeypatch.setattr(influencer_discovery.HTTP, "post", fake_post)

    got = _client().discover(filters={}, target=100)
    assert len(got) == 2
    assert calls["n"] == 1


# --- exclude_handles ---------------------------------------------------------

def test_exclude_handles_are_normalized_and_sent(monkeypatch):
    sent = {}

    def fake_post(url, json=None, **k):
        sent["payload"] = json
        return _resp([_account("@fresh")])

    monkeypatch.setattr(influencer_discovery.HTTP, "post", fake_post)

    _client().discover(
        filters={"profile_language": ["en"]},
        target=1,
        exclude_handles=["@Known", "https://youtube.com/@Other", "", "@DUPE", "@dupe"],
    )
    excl = sent["payload"]["filters"]["exclude_handles"]
    assert excl == ["dupe", "known", "other"]   # normalized, deduped, sorted, no empties
    # base filters preserved alongside the injected exclusion
    assert sent["payload"]["filters"]["profile_language"] == ["en"]
    assert sent["payload"]["platform"] == "youtube"
    assert sent["payload"]["paging"]["limit"] == influencer_discovery.PAGE_LIMIT


def test_exclude_handles_over_the_cap_are_truncated_with_a_warning(monkeypatch, caplog):
    sent = {}

    def fake_post(url, json=None, **k):
        sent["payload"] = json
        return _resp([_account("@fresh")])

    monkeypatch.setattr(influencer_discovery.HTTP, "post", fake_post)
    monkeypatch.setattr(influencer_discovery, "INFLUENCERS_MAX_EXCLUDE_HANDLES", 10)

    handles = [f"@h{i}" for i in range(25)]
    with caplog.at_level(logging.WARNING):
        _client().discover(filters={}, target=1, exclude_handles=handles)

    assert len(sent["payload"]["filters"]["exclude_handles"]) == 10
    assert any("over the" in r.message and "cap" in r.message for r in caplog.records)


def test_already_bare_handles_are_kept_not_dropped(monkeypatch):
    """The blocklist and external-dedupe indexes store BARE handles (no '@').
    normalize_handle() returns '' for those, so without the strip/lower
    fallback the whole DO NOT CONTACT exclusion would silently vanish."""
    sent = {}

    def fake_post(url, json=None, **k):
        sent["payload"] = json
        return _resp([_account("@fresh")])

    monkeypatch.setattr(influencer_discovery.HTTP, "post", fake_post)

    _client().discover(
        filters={}, target=1,
        exclude_handles=["blockedcreator", "AgencyHandle", "@withat"],
    )
    assert sent["payload"]["filters"]["exclude_handles"] == ["agencyhandle", "blockedcreator", "withat"]


def test_no_exclude_key_when_the_set_is_empty(monkeypatch):
    sent = {}

    def fake_post(url, json=None, **k):
        sent["payload"] = json
        return _resp([_account("@a")])

    monkeypatch.setattr(influencer_discovery.HTTP, "post", fake_post)

    _client().discover(filters={"profile_language": ["en"]}, target=1, exclude_handles=[])
    assert "exclude_handles" not in sent["payload"]["filters"]


# --- fail-soft ---------------------------------------------------------------

def test_non_200_returns_empty_without_raising(monkeypatch):
    monkeypatch.setattr(influencer_discovery.HTTP, "post", lambda *a, **k: _Resp(500, {}))
    assert _client().discover(filters={}, target=5) == []


def test_transport_error_returns_empty_without_raising(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(influencer_discovery.HTTP, "post", boom)
    assert _client().discover(filters={}, target=5) == []


def test_non_json_200_returns_empty(monkeypatch):
    monkeypatch.setattr(influencer_discovery.HTTP, "post", lambda *a, **k: _Resp(200, _BAD_JSON))
    assert _client().discover(filters={}, target=5) == []


def test_partial_before_a_failing_page_is_kept(monkeypatch):
    monkeypatch.setattr(influencer_discovery, "PAGE_LIMIT", 2)
    pages = [_resp([_account("@a"), _account("@b")], total=100), _Resp(503, {})]
    monkeypatch.setattr(influencer_discovery.HTTP, "post", lambda *a, **k: pages.pop(0))

    got = _client().discover(filters={}, target=10)
    assert sorted(c["handle"] for c in got) == ["a", "b"]   # page 1 kept, page 2 dropped


# --- credit accounting + ceiling --------------------------------------------

def test_credits_spent_accumulates_from_credits_cost(monkeypatch):
    monkeypatch.setattr(influencer_discovery, "PAGE_LIMIT", 2)
    pages = [
        _resp([_account("@a"), _account("@b")], total=100, credits_cost=0.02, credits_left=9.5),
        _resp([_account("@c"), _account("@d")], total=100, credits_cost=0.02, credits_left=9.48),
    ]
    monkeypatch.setattr(influencer_discovery.HTTP, "post", lambda *a, **k: pages.pop(0))

    client = _client()
    client.discover(filters={}, target=4)
    assert round(client.credits_spent, 4) == 0.04
    assert client.credits_left_reported == 9.48


def test_credit_ceiling_stops_pagination(monkeypatch):
    monkeypatch.setattr(influencer_discovery, "PAGE_LIMIT", 2)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        # full page, huge supply -> only the ceiling can stop the loop
        return _resp([_account(f"@a{calls['n']}"), _account(f"@b{calls['n']}")],
                     total=10_000, credits_cost=0.02)

    monkeypatch.setattr(influencer_discovery.HTTP, "post", fake_post)

    client = _client(max_credits=0.02)   # room for exactly one page
    client.discover(filters={}, target=1000)
    assert calls["n"] == 1
    assert client.credits_spent == 0.02


# --- soft-disable ------------------------------------------------------------

def test_from_config_is_inert_without_a_key(monkeypatch):
    monkeypatch.setattr(influencer_discovery, "INFLUENCERS_API_KEY", None)
    client = InfluencerDiscovery.from_config()
    assert client.enabled is False
    # even with a live-looking HTTP, a disabled client never calls it
    monkeypatch.setattr(influencer_discovery.HTTP, "post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")))
    assert client.discover(filters={}, target=5) == []


def test_null_discovery_returns_nothing(monkeypatch):
    monkeypatch.setattr(influencer_discovery.HTTP, "post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")))
    assert null_discovery().discover(filters={}, target=5) == []


def test_target_zero_short_circuits(monkeypatch):
    monkeypatch.setattr(influencer_discovery.HTTP, "post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")))
    assert _client().discover(filters={}, target=0) == []
