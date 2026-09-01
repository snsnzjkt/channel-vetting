"""
The vendor's fair-use HANDLE meter has to be enforced, not merely recorded.

Context, because this is the second meter and the tests only make sense against
it: influencers.club bills 0.01 credits per creator RETURNED, and SEPARATELY
caps how many creators ("handles") the Discovery API will return per billing
period. On 2026-09-01 it mailed to say we had used 5,042 of 5,000 — while every
credit ceiling in `credit_tracker` was reading green, because credits and
handles are different meters and email enrichment spends the former and none of
the latter.

So these tests pin four things:
  1. handles are counted off `len(accounts)`, not derived from the credit cost;
  2. the cap stops the NEXT page before it is bought, projecting a full page;
  3. the count survives the process, since a billing period outlives a run;
  4. an exhausted allowance turns discovery off cleanly rather than leaving a
     client that says it is enabled and then buys nothing.

`tests/conftest.py`'s autouse `isolate_credit_ledger` keeps all of this off the
production ledger and lifts the cap by default; every test here sets its own.
"""
import pytest

from channel_vetting.budget import credit_tracker
from channel_vetting.budget.credit_tracker import (
    KIND_DISCOVERY,
    can_afford_handles,
    handles_this_period,
    record_spend,
)
from channel_vetting.discovery import influencers_club


class _Resp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = ""
        self.headers = {}

    def json(self):
        return self._payload


def _account(handle):
    return {"profile": {"username": handle, "full_name": "Someone"}, "user_id": handle}


def _page(n, credits_cost=0.5, total=10_000):
    return _Resp({
        "accounts": [_account(f"@a{i}") for i in range(n)],
        "total": total,
        "credits_cost": credits_cost,
    })


def _client(**kw):
    kw.setdefault("max_credits", 10_000.0)  # per-run credit ceiling out of the way
    return influencers_club.InfluencerDiscovery(sleep=lambda *_: None, **kw)


@pytest.fixture
def cap(monkeypatch):
    """Set the handle allowance for one test."""
    def _set(n):
        monkeypatch.setattr(
            credit_tracker, "INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_PERIOD", n
        )
    return _set


# --- counting --------------------------------------------------------------

def test_handles_are_counted_off_the_accounts_returned(monkeypatch):
    monkeypatch.setattr(influencers_club, "PAGE_LIMIT", 3)
    monkeypatch.setattr(
        influencers_club.HTTP, "post", lambda *a, **k: _page(3, total=3),
    )

    _client().discover(filters={}, target=3)

    assert handles_this_period() == 3


def test_the_handle_count_is_not_derived_from_the_credit_cost(monkeypatch):
    """
    The two agree at the observed 0.01/creator rate, so a derived count would
    pass a naive test. Bill an ABSURD price for one account: handles must follow
    the accounts, because the rate is our measurement and the meter is theirs.
    """
    monkeypatch.setattr(influencers_club, "PAGE_LIMIT", 1)
    monkeypatch.setattr(
        influencers_club.HTTP, "post",
        lambda *a, **k: _page(1, credits_cost=7.5, total=1),
    )

    _client().discover(filters={}, target=1)

    assert handles_this_period() == 1
    assert credit_tracker.credits_today() == 7.5


def test_email_credits_consume_no_handles():
    """The whole reason this is a separate meter."""
    record_spend(2.4, kind=credit_tracker.KIND_EMAIL, detail="addresses")

    assert credit_tracker.credits_today() == 2.4
    assert handles_this_period() == 0


# --- enforcement -----------------------------------------------------------

def test_the_allowance_stops_the_next_page(monkeypatch, cap):
    """
    Projected, not balance-only. With a 120 cap and 50-creator pages, the third
    page would land on 150 — so it must never be bought, leaving 100.
    """
    cap(120)
    monkeypatch.setattr(influencers_club, "PAGE_LIMIT", 50)
    calls = []

    def _post(*a, **k):
        calls.append(1)
        return _page(50)

    monkeypatch.setattr(influencers_club.HTTP, "post", _post)

    _client().discover(filters={}, target=10_000)

    assert len(calls) == 2
    assert handles_this_period() == 100


def test_the_allowance_survives_the_process(monkeypatch, cap):
    """
    A billing period outlives a run, so a SECOND client must inherit the first
    one's spend. This is the failure the per-run counter could never catch: two
    runs a day each got a full allowance.
    """
    cap(120)
    monkeypatch.setattr(influencers_club, "PAGE_LIMIT", 50)
    monkeypatch.setattr(influencers_club.HTTP, "post", lambda *a, **k: _page(50))

    _client().discover(filters={}, target=10_000)
    assert handles_this_period() == 100

    calls = []

    def _post(*a, **k):
        calls.append(1)
        return _page(50)

    monkeypatch.setattr(influencers_club.HTTP, "post", _post)
    second = _client()

    assert second.enabled is False
    assert second.discover(filters={}, target=10_000) == []
    assert calls == []
    assert handles_this_period() == 100


def test_an_exhausted_allowance_reports_the_client_as_disabled(monkeypatch, cap):
    """
    run_niche reads `enabled` as `use_discovery`, and a discovery_source="both"
    niche keeps its full keyword list when that is False — so the free YouTube
    loop picks up the slack. A client that claimed to be enabled and then bought
    nothing would strand the niche with neither source.
    """
    cap(10)
    monkeypatch.setattr(influencers_club, "PAGE_LIMIT", 50)

    assert _client().enabled is False


def test_probe_spends_from_the_same_meter(monkeypatch, cap):
    cap(5)
    monkeypatch.setattr(influencers_club.HTTP, "post", lambda *a, **k: _page(3, total=3))

    client = _client()
    accounts, _ = client.probe({}, limit=3)
    assert len(accounts) == 3
    assert handles_this_period() == 3

    # 3 + 3 > 5, so the second probe must be refused before it is sent.
    assert client.probe({}, limit=3) == (None, None)
    assert handles_this_period() == 3


def test_an_unreadable_ledger_refuses_to_spend_handles(monkeypatch, tmp_path):
    """
    Fails CLOSED, matching can_afford. Not knowing the balance means not buying.
    """
    bad = tmp_path / "corrupt.json"
    bad.write_text("{not json")
    monkeypatch.setattr(credit_tracker, "CREDIT_LOG_FILE", str(bad))

    assert can_afford_handles(50, "discovery") is False


# --- the window ------------------------------------------------------------

def test_spend_outside_the_rolling_window_stops_counting(monkeypatch, cap):
    """
    The window is trailing, so an old period's spend must age out — otherwise
    discovery never restarts after a single over-limit month.
    """
    cap(120)
    monkeypatch.setattr(credit_tracker, "INFLUENCERS_HANDLE_PERIOD_DAYS", 31)
    monkeypatch.setattr(credit_tracker, "today_iso", lambda: "2026-09-02")

    log = credit_tracker.load_log()
    log["days"]["2026-07-01"] = {"total": 1.0, "by_kind": {}, "handles": 5000}
    log["days"]["2026-09-01"] = {"total": 1.0, "by_kind": {}, "handles": 40}
    credit_tracker._save_log(log)

    assert handles_this_period() == 40
    assert can_afford_handles(50, "discovery") is True


def test_an_anchored_period_start_overrides_the_rolling_window(monkeypatch, cap):
    """
    Once the vendor tells us the renewal date, counting from it is both more
    accurate and less conservative than a trailing window.
    """
    cap(120)
    monkeypatch.setattr(credit_tracker, "today_iso", lambda: "2026-09-02")
    monkeypatch.setattr(credit_tracker, "INFLUENCERS_HANDLE_PERIOD_START", "2026-09-01")

    log = credit_tracker.load_log()
    log["days"]["2026-08-30"] = {"total": 1.0, "by_kind": {}, "handles": 5000}
    log["days"]["2026-09-01"] = {"total": 1.0, "by_kind": {}, "handles": 40}
    credit_tracker._save_log(log)

    assert handles_this_period() == 40


def test_a_bad_period_start_falls_back_to_the_rolling_window(monkeypatch, cap):
    """A typo in a date must never widen the cap."""
    cap(120)
    monkeypatch.setattr(credit_tracker, "today_iso", lambda: "2026-09-02")
    monkeypatch.setattr(credit_tracker, "INFLUENCERS_HANDLE_PERIOD_START", "not-a-date")

    log = credit_tracker.load_log()
    log["days"]["2026-08-30"] = {"total": 1.0, "by_kind": {}, "handles": 5000}
    credit_tracker._save_log(log)

    assert handles_this_period() == 5000
    assert can_afford_handles(50, "discovery") is False


def test_the_summary_shows_both_meters(monkeypatch, cap):
    """
    2026-09-01 happened because a green credit figure was the only figure on
    screen. The handle count has to sit beside it.
    """
    cap(4500)
    record_spend(0.5, kind=KIND_DISCOVERY, detail="page", handles=50)

    summary = credit_tracker.spend_summary()
    assert "handles 50/4500" in summary
