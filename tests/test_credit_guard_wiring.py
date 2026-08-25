"""
The ledger has to actually GATE the two paid paths, not merely observe them.

A tracker nothing consults is a report, not a guard. These tests pin the wiring
at both call sites — discovery (0.01 per creator returned, billed before any
gate sees them) and the email enrich step (0.2 per validated address) — plus the
two things that make the guard trustworthy: it stops paying when the ledger
cannot be read, and it records the VENDOR's figure rather than our estimate.

`tests/conftest.py`'s autouse `isolate_credit_ledger` fixture keeps all of this
off the production ledger.
"""
import pytest
import requests

from channel_vetting.budget import credit_tracker
from channel_vetting.discovery import influencers_club
from channel_vetting.enrichment import email_influencers
from channel_vetting.budget.credit_tracker import (
    KIND_DISCOVERY,
    KIND_EMAIL,
    credits_today,
    record_spend,
)


# --- helpers ---------------------------------------------------------------

class _Resp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = ""
        self.headers = {}

    def json(self):
        return self._payload


def _account(handle):
    """
    The vendor's real account shape. `_to_candidate` reads
    `account["profile"]["username"]`, NOT a flat `handle` — an earlier version of
    this helper used flat keys and every candidate silently became None.
    """
    return {"profile": {"username": handle, "full_name": "Someone"}, "user_id": handle}


def _discovery_resp(accounts, total=10_000, credits_cost=0.5, credits_left=None):
    """
    `_parse` reads `accounts`/`total` at the TOP LEVEL of the body. An earlier
    version of this helper nested them under "data", so `_parse` returned an
    empty page, `discover()` broke at `if not accounts` after ONE page, and the
    across-runs ceiling test passed while proving nothing.
    """
    body = {"accounts": accounts, "total": total, "credits_cost": credits_cost}
    if credits_left is not None:
        body["credits_left"] = credits_left
    return _Resp(body)


def _discovery_client(**kw):
    kw.setdefault("max_credits", 100.0)   # per-run ceiling out of the way
    return influencers_club.InfluencerDiscovery(sleep=lambda *_: None, **kw)


@pytest.fixture
def tight_day(monkeypatch):
    monkeypatch.setattr(credit_tracker, "INFLUENCERS_MAX_CREDITS_PER_DAY", 1.0)
    monkeypatch.setattr(credit_tracker, "INFLUENCERS_MAX_CREDITS_PER_MONTH", 100.0)


# --- discovery -------------------------------------------------------------

def test_discovery_records_the_vendors_own_cost(monkeypatch):
    monkeypatch.setattr(influencers_club, "PAGE_LIMIT", 2)
    monkeypatch.setattr(
        influencers_club.HTTP, "post",
        lambda *a, **k: _discovery_resp([_account("@a"), _account("@b")],
                                        total=2, credits_cost=0.02),
    )

    _discovery_client().discover(filters={}, target=2)

    assert credits_today() == 0.02
    log = credit_tracker.load_log()["days"][credit_tracker.today_iso()]
    assert log["by_kind"] == {KIND_DISCOVERY: 0.02}


def test_the_fixture_payload_actually_parses(monkeypatch):
    """
    Guard on the guards. Every discovery test below is meaningless if the fake
    response does not parse: `_parse` would return an empty page and `discover()`
    would break at `if not accounts` after one page, so a ceiling test would pass
    without a ceiling ever firing. Assert candidates come back BEFORE trusting
    any test that counts pages.
    """
    monkeypatch.setattr(influencers_club, "PAGE_LIMIT", 2)
    monkeypatch.setattr(
        influencers_club.HTTP, "post",
        lambda *a, **k: _discovery_resp([_account("@one"), _account("@two")], total=2),
    )

    got = _discovery_client().discover(filters={}, target=2)
    assert [c["handle"] for c in got] == ["one", "two"]
    assert got[0]["channel_title"] == "Someone"


def test_the_daily_ceiling_stops_discovery_across_runs(monkeypatch, tight_day):
    """The point of the ledger: a SECOND run in the same day inherits the first
    run's spend instead of getting a fresh allowance."""
    monkeypatch.setattr(influencers_club, "PAGE_LIMIT", 2)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        # A FULL page (PAGE_LIMIT items) and huge `total`, so supply never
        # exhausts and only a ceiling can end the loop.
        return _discovery_resp(
            [_account(f"@a{calls['n']}"), _account(f"@b{calls['n']}")],
            total=10_000, credits_cost=0.5,
        )

    monkeypatch.setattr(influencers_club.HTTP, "post", fake_post)

    # Run 1: the 1.0 daily ceiling allows exactly two 0.5 pages.
    got1 = _discovery_client().discover(filters={}, target=1000)
    assert calls["n"] == 2, "run 1 should page until the daily ceiling, not stop early"
    assert len(got1) == 4                      # proves the payload really parsed
    assert credits_today() == 1.0

    # Run 2: a brand-new client. The old per-run counter started at 0 here and
    # would have bought two more pages; the ledger must refuse every one.
    got2 = _discovery_client().discover(filters={}, target=1000)

    assert calls["n"] == 2, "run 2 bought a page against an exhausted daily budget"
    assert got2 == []
    assert credits_today() == 1.0


def test_the_vendors_own_balance_outranks_our_ceilings(monkeypatch):
    """`credits_left` arrives free on every discovery response and used to reach
    only a log line. It is the entitlement; our monthly ceiling is a guess."""
    monkeypatch.setattr(influencers_club, "PAGE_LIMIT", 2)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _discovery_resp(
            [_account(f"@a{calls['n']}"), _account(f"@b{calls['n']}")],
            total=10_000, credits_cost=0.02, credits_left=0.01,
        )

    monkeypatch.setattr(influencers_club.HTTP, "post", fake_post)

    _discovery_client().discover(filters={}, target=1000)

    # One page bought, the vendor then reports 0.01 left, and a 0.02 page is
    # refused even though both of OUR ceilings have plenty of room.
    assert calls["n"] == 1
    assert credit_tracker.load_log()["vendor_credits_left"] == 0.01
    assert credits_today() == 0.02


def test_the_vendor_balance_also_gates_the_email_step(monkeypatch):
    """The email step never sees a discovery response, so it can only benefit
    from the vendor's balance if the ledger carries it across."""
    log = credit_tracker.load_log()
    log["vendor_credits_left"] = 0.1        # less than one 0.2 hit
    credit_tracker._save_log(log)

    posted = {"n": 0}

    def fake_post(*a, **k):
        posted["n"] += 1
        return _Resp({"result": {"email": "a@b.com"}, "credits_cost": 0.2})

    monkeypatch.setattr(email_influencers.HTTP, "post", fake_post)

    assert email_influencers.InfluencersClient(sleep=lambda *_: None).find_email("UC1") == ""
    assert posted["n"] == 0


def test_discovery_stops_when_the_ledger_cannot_be_read(monkeypatch, tmp_path):
    """Refusing to spend beats guessing zero: an unreadable ledger means we do
    not know the balance."""
    bad = tmp_path / "corrupt.json"
    bad.write_text("{{{", encoding="utf-8")
    monkeypatch.setattr(credit_tracker, "CREDIT_LOG_FILE", str(bad))

    posted = {"n": 0}

    def fake_post(*a, **k):
        posted["n"] += 1
        return _discovery_resp([_account("@a")])

    monkeypatch.setattr(influencers_club.HTTP, "post", fake_post)

    client = _discovery_client()
    assert client.discover(filters={}, target=50) == []
    assert posted["n"] == 0, "bought a page against an unknown balance"
    assert client.enabled is False


def test_a_page_is_projected_before_it_is_bought(monkeypatch, tight_day):
    """0.9 of a 1.0 ceiling must refuse a 0.5 page. The old `spent >= max` test
    authorised it and overshot by a page, per niche."""
    monkeypatch.setattr(influencers_club, "PAGE_LIMIT", 50)
    record_spend(0.9, kind=KIND_DISCOVERY)

    posted = {"n": 0}

    def fake_post(*a, **k):
        posted["n"] += 1
        return _discovery_resp([_account(f"@a{posted['n']}")])

    monkeypatch.setattr(influencers_club.HTTP, "post", fake_post)

    _discovery_client().discover(filters={}, target=1000)
    assert posted["n"] == 0
    assert credits_today() == 0.9


# --- email enrich ----------------------------------------------------------

def _email_client(monkeypatch, payload, status_code=200):
    monkeypatch.setattr(
        email_influencers.HTTP, "post", lambda *a, **k: _Resp(payload, status_code)
    )
    return email_influencers.InfluencersClient(sleep=lambda *_: None)


def test_an_email_hit_is_recorded_from_the_vendors_figure(monkeypatch):
    client = _email_client(monkeypatch, {
        "result": {"email": "creator@example.com"},
        "credits_cost": 0.2,
    })

    assert client.find_email("UC1") == "creator@example.com"
    assert credits_today() == 0.2
    log = credit_tracker.load_log()["days"][credit_tracker.today_iso()]
    assert log["by_kind"] == {KIND_EMAIL: 0.2}


def test_a_free_miss_is_not_recorded(monkeypatch):
    """`must_have` bills nothing for an empty result, and step 4 only runs for
    channels the free steps missed — so misses are the common case. Charging
    them would turn a credit budget into a request cap."""
    client = _email_client(monkeypatch, {"result": {}, "credits_cost": 0})

    assert client.find_email("UC1") == ""
    assert credits_today() == 0.0


def test_the_daily_ceiling_refuses_a_lookup_it_could_not_pay_for(monkeypatch, tight_day):
    """Refuses only in the last 0.2 of headroom — the price of one hit."""
    record_spend(0.9, kind=KIND_DISCOVERY)      # 0.1 left of 1.0

    posted = {"n": 0}

    def fake_post(*a, **k):
        posted["n"] += 1
        return _Resp({"result": {"email": "a@b.com"}, "credits_cost": 0.2})

    monkeypatch.setattr(email_influencers.HTTP, "post", fake_post)

    client = email_influencers.InfluencersClient(sleep=lambda *_: None)
    assert client.find_email("UC1") == ""
    assert posted["n"] == 0, "sent a lookup it could not afford"
    assert client.enabled is False


def test_headroom_for_exactly_one_hit_still_allows_the_lookup(monkeypatch, tight_day):
    """The gate must not be over-conservative: 0.2 left is enough for a hit."""
    record_spend(0.8, kind=KIND_DISCOVERY)      # exactly 0.2 left

    client = _email_client(monkeypatch, {
        "result": {"email": "a@b.com"}, "credits_cost": 0.2,
    })
    assert client.find_email("UC1") == "a@b.com"
    assert credits_today() == 1.0


def test_email_lookups_stop_when_the_ledger_cannot_be_read(monkeypatch, tmp_path):
    bad = tmp_path / "corrupt.json"
    bad.write_text("nope", encoding="utf-8")
    monkeypatch.setattr(credit_tracker, "CREDIT_LOG_FILE", str(bad))

    posted = {"n": 0}

    def fake_post(*a, **k):
        posted["n"] += 1
        return _Resp({"result": {"email": "a@b.com"}, "credits_cost": 0.2})

    monkeypatch.setattr(email_influencers.HTTP, "post", fake_post)

    client = email_influencers.InfluencersClient(sleep=lambda *_: None)
    assert client.find_email("UC1") == ""
    assert posted["n"] == 0
    assert client.enabled is False


def test_a_rejected_address_is_still_charged(monkeypatch):
    """The vendor bills when it returns an address; our fullmatch/blocklist
    screens are policy applied after the fact and do not refund it."""
    client = _email_client(monkeypatch, {
        "result": {"email": "contact us at a@b.com today"},   # fails fullmatch
        "credits_cost": 0.2,
    })

    assert client.find_email("UC1") == ""
    assert credits_today() == 0.2


# --- the inert clients must not touch the ledger ---------------------------

def test_a_disabled_client_spends_nothing(monkeypatch):
    """No API key means no paid calls, and therefore no ledger writes."""
    assert influencers_club.InfluencerDiscovery(enabled=False).discover(
        filters={}, target=50
    ) == []
    assert email_influencers.InfluencersClient(enabled=False).find_email("UC1") == ""
    assert credits_today() == 0.0
