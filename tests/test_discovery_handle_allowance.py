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
     client that says it is enabled and then buys nothing;
  5. the INTRODUCTORY cap (a lower ceiling that expires on a date and reverts to
     the steady-state one) is what actually gets enforced while it is in force.

`tests/conftest.py`'s autouse `isolate_credit_ledger` keeps all of this off the
production ledger and lifts the cap by default; every test here sets its own.
"""
import logging
from datetime import date

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
        # Keep the introductory cap out of the way so `n` is the cap the test
        # asked for, not min(n, 3965). The intro window has its own tests.
        monkeypatch.setattr(credit_tracker, "INFLUENCERS_HANDLE_INTRO_UNTIL", "")
        # Lift the DAILY pace cap too. It is derived as n // 31, so a test that
        # sets a small period cap to watch the PERIOD refuse would otherwise be
        # refused a page earlier, by a different ceiling, and would no longer be
        # testing what its name says. The daily cap has its own tests below.
        monkeypatch.setattr(
            credit_tracker, "INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_DAY", "10000000"
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
    # BOTH handle ceilings, not just the period one: a day that is pacing-capped
    # while the period still has thousands free is the same class of invisible
    # state that 2026-09-01 was, one level down.
    assert "50/4500 in 31d" in summary
    assert "50/" in summary and "today" in summary


# --- the introductory cap ---------------------------------------------------
#
# Two numbers with a date boundary between them: 3,965 until
# INFLUENCERS_HANDLE_INTRO_UNTIL, 4,500 from that date on. The boundary is
# EXCLUSIVE, and it is resolved per call so a long run crossing midnight picks
# up the new value.

@pytest.fixture
def intro(monkeypatch):
    """Set the introductory cap and its expiry for one test."""
    def _set(cap, until, steady=4500):
        monkeypatch.setattr(credit_tracker, "INFLUENCERS_HANDLE_INTRO_CAP", cap)
        monkeypatch.setattr(credit_tracker, "INFLUENCERS_HANDLE_INTRO_UNTIL", until)
        monkeypatch.setattr(
            credit_tracker, "INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_PERIOD", steady
        )
    return _set


def test_intro_cap_applies_before_the_boundary(intro):
    intro(3965, "2026-10-12")
    assert credit_tracker.discovery_handle_cap(date(2026, 9, 12)) == 3965
    assert credit_tracker.discovery_handle_cap(date(2026, 10, 11)) == 3965


def test_steady_cap_applies_from_the_boundary_on(intro):
    """The boundary date itself is already the steady-state cap, not the last
    day of the introductory one."""
    intro(3965, "2026-10-12")
    assert credit_tracker.discovery_handle_cap(date(2026, 10, 12)) == 4500
    assert credit_tracker.discovery_handle_cap(date(2026, 11, 1)) == 4500


def test_empty_until_retires_the_intro_cap(intro):
    intro(3965, "")
    assert credit_tracker.discovery_handle_cap(date(2026, 9, 12)) == 4500


def test_unparseable_until_falls_back_to_the_lower_cap(intro, caplog):
    """A spend guard that cannot be read must not authorise MORE spend."""
    intro(3965, "not-a-date")
    with caplog.at_level(logging.WARNING):
        assert credit_tracker.discovery_handle_cap(date(2026, 9, 12)) == 3965
    assert "not a YYYY-MM-DD date" in caplog.text


def test_intro_cap_is_what_can_afford_handles_enforces(intro, monkeypatch):
    """The resolver is not decorative: the introductory number is the one that
    actually blocks a discovery page."""
    intro(3965, "2026-10-12")
    monkeypatch.setattr(credit_tracker, "_handles_in_window", lambda _log: 3900)
    monkeypatch.setattr(credit_tracker, "load_log", lambda: {"days": {}})

    assert credit_tracker.can_afford_handles(50) is True    # 3950, under 3965
    assert credit_tracker.can_afford_handles(100) is False  # 4000, over 3965


# --- the DAILY pace cap -----------------------------------------------------
#
# The period cap bounds a TOTAL; it permits the whole allowance to go in the
# first days of a period and leave discovery dark behind a vendor 429 for the
# rest. The daily cap bounds the RATE, and is derived from the period cap so
# there is no second number to keep in sync.

@pytest.fixture
def day_cap(monkeypatch):
    """Set the period cap and let the daily one derive from it."""
    def _set(period, window=31, override=""):
        monkeypatch.setattr(credit_tracker, "INFLUENCERS_HANDLE_INTRO_UNTIL", "")
        monkeypatch.setattr(
            credit_tracker, "INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_PERIOD", period
        )
        monkeypatch.setattr(credit_tracker, "INFLUENCERS_HANDLE_PERIOD_DAYS", window)
        monkeypatch.setattr(
            credit_tracker, "INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_DAY", override
        )
    return _set


def test_daily_cap_is_derived_from_the_period_cap(day_cap):
    day_cap(4500)
    assert credit_tracker.discovery_handle_cap_per_day() == 145   # 4500 // 31


def test_daily_cap_follows_the_introductory_period_cap(monkeypatch):
    """The whole point of deriving it: the introductory cap moves the daily one
    with it, and the 2026-10-12 step-up needs no second date boundary."""
    monkeypatch.setattr(credit_tracker, "INFLUENCERS_HANDLE_INTRO_CAP", 3965)
    monkeypatch.setattr(
        credit_tracker, "INFLUENCERS_HANDLE_INTRO_UNTIL", "2026-10-12"
    )
    monkeypatch.setattr(
        credit_tracker, "INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_PERIOD", 4500
    )
    monkeypatch.setattr(credit_tracker, "INFLUENCERS_HANDLE_PERIOD_DAYS", 31)
    monkeypatch.setattr(credit_tracker, "INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_DAY", "")

    assert credit_tracker.discovery_handle_cap_per_day(date(2026, 9, 12)) == 127
    assert credit_tracker.discovery_handle_cap_per_day(date(2026, 10, 12)) == 145


def test_a_full_window_of_daily_caps_cannot_exceed_the_period_cap(day_cap):
    """Floor division is load-bearing: this is the property that would break if
    the derivation ever rounded up."""
    for period in (3965, 4500, 5000, 1, 0):
        day_cap(period)
        per_day = credit_tracker.discovery_handle_cap_per_day()
        assert per_day * 31 <= period


def test_daily_cap_refuses_the_page_that_would_cross_it(day_cap, monkeypatch):
    day_cap(3965)  # -> 127/day
    monkeypatch.setattr(credit_tracker, "_handles_today", lambda _log: 100)
    monkeypatch.setattr(credit_tracker, "_handles_in_window", lambda _log: 100)
    monkeypatch.setattr(credit_tracker, "load_log", lambda: {"days": {}})

    assert credit_tracker.can_afford_handles(27) is True    # 127, exactly at cap
    assert credit_tracker.can_afford_handles(28) is False   # 128, over


def test_daily_refusal_names_the_daily_cap_not_the_period_one(day_cap, monkeypatch, caplog):
    """A refusal about pacing must not read as an exhausted allowance — they
    have different remedies (wait a day vs wait for renewal)."""
    day_cap(3965)
    monkeypatch.setattr(credit_tracker, "_handles_today", lambda _log: 127)
    monkeypatch.setattr(credit_tracker, "_handles_in_window", lambda _log: 127)
    monkeypatch.setattr(credit_tracker, "load_log", lambda: {"days": {}})

    with caplog.at_level(logging.WARNING):
        assert credit_tracker.can_afford_handles(50) is False
    assert "daily handle cap" in caplog.text


def test_period_cap_still_refuses_even_when_the_day_is_clear(day_cap, monkeypatch):
    """Both ceilings hold. A fresh day does not unlock an exhausted period."""
    day_cap(3965)
    monkeypatch.setattr(credit_tracker, "_handles_today", lambda _log: 0)
    monkeypatch.setattr(credit_tracker, "_handles_in_window", lambda _log: 3950)
    monkeypatch.setattr(credit_tracker, "load_log", lambda: {"days": {}})

    assert credit_tracker.can_afford_handles(50) is False


def test_explicit_override_beats_the_derivation(day_cap):
    day_cap(3965, override="180")
    assert credit_tracker.discovery_handle_cap_per_day() == 180


def test_unparseable_override_falls_back_to_the_derivation(day_cap, caplog):
    day_cap(3965, override="lots")
    with caplog.at_level(logging.WARNING):
        assert credit_tracker.discovery_handle_cap_per_day() == 127
    assert "not a whole number" in caplog.text


def test_zero_override_stops_paid_discovery(day_cap, monkeypatch):
    day_cap(3965, override="0")
    monkeypatch.setattr(credit_tracker, "_handles_today", lambda _log: 0)
    monkeypatch.setattr(credit_tracker, "_handles_in_window", lambda _log: 0)
    monkeypatch.setattr(credit_tracker, "load_log", lambda: {"days": {}})

    assert credit_tracker.can_afford_handles(1) is False


def test_handles_today_reads_the_current_day_only(monkeypatch):
    from channel_vetting.core.prospect_day import today_iso

    log = {"days": {today_iso(): {"handles": 42}, "2026-01-01": {"handles": 999}}}
    assert credit_tracker._handles_today(log) == 42


def test_malformed_handles_today_reads_as_zero_not_as_headroom(monkeypatch):
    from channel_vetting.core.prospect_day import today_iso

    assert credit_tracker._handles_today({"days": {today_iso(): {"handles": "x"}}}) == 0
    assert credit_tracker._handles_today({"days": {}}) == 0
