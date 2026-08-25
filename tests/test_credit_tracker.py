"""
The credit ledger is the only thing standing between a mistake and a bill.

Credits are the one spend in this pipeline that is real money, and until this
module existed they were the only spend with no ledger: the counter lived on an
`InfluencerDiscovery` instance, so it described one run and vanished with the
process. Two runs on the same day each got a full allowance, and nothing could
report a monthly total.

The rules pinned here:

1. Spend PERSISTS across "runs" (fresh reads of the same file).
2. Limits are checked against the PROJECTED total — balance plus the price of the
   next call. See `can_afford` for why comparing the balance alone is not a
   ceiling.
3. Three limits, and the VENDOR's own reported balance outranks both of ours.
4. An unreadable ledger never reads as zero. `assert_readable()` raises at run
   start; mid-run the helpers return False/0.0 so one corrupt read stops paid
   calls without unwinding a run that has already spent YouTube quota.
5. Writes are atomic, and a zero cost is a no-op.

`tests/conftest.py`'s autouse `isolate_credit_ledger` points CREDIT_LOG_FILE at a
temp file and lifts the ceilings for the whole suite; ceiling tests here opt back
in via the `credit_ceilings` factory.
"""
import json

import pytest

from channel_vetting.budget import credit_tracker
from channel_vetting.budget.credit_tracker import (
    KIND_DISCOVERY,
    KIND_EMAIL,
    CreditLedgerUnavailable,
    assert_readable,
    can_afford,
    credits_this_month,
    credits_today,
    record_spend,
    record_vendor_balance,
    spend_summary,
)


@pytest.fixture
def corrupt_ledger(monkeypatch, tmp_path):
    """A ledger file that exists and does not parse."""
    def _make(body="{not json at all"):
        bad = tmp_path / "corrupt.json"
        bad.write_text(body, encoding="utf-8")
        monkeypatch.setattr(credit_tracker, "CREDIT_LOG_FILE", str(bad))
        return bad
    return _make


# --- persistence: the reason this module exists -----------------------------

def test_an_empty_ledger_starts_at_zero():
    assert credits_today() == 0.0
    assert credits_this_month() == 0.0


def test_spend_survives_a_fresh_read():
    """The per-run counter this replaces lived on an object and reset with the
    process, so a second run in a day got a whole fresh allowance."""
    assert record_spend(0.5, kind=KIND_DISCOVERY) is True
    assert credits_today() == 0.5

    # A different "run" reading the same file sees the earlier spend.
    assert credit_tracker.load_log()["days"][credit_tracker.today_iso()]["total"] == 0.5
    record_spend(0.25, kind=KIND_DISCOVERY)
    assert credits_today() == 0.75


def test_spend_is_split_by_kind():
    """An undifferentiated total cannot say WHICH stream is drifting, and the two
    bill at 20x different rates (0.01/creator vs 0.2/address)."""
    record_spend(0.5, kind=KIND_DISCOVERY)
    record_spend(0.2, kind=KIND_EMAIL)

    by_kind = credit_tracker.load_log()["days"][credit_tracker.today_iso()]["by_kind"]
    assert by_kind == {KIND_DISCOVERY: 0.5, KIND_EMAIL: 0.2}
    assert credits_today() == 0.7


def test_the_month_total_accumulates_alongside_the_day():
    record_spend(0.4, kind=KIND_DISCOVERY)
    record_spend(0.6, kind=KIND_EMAIL)
    assert credits_this_month() == 1.0


@pytest.mark.parametrize("cost", [0, 0.0, -1, -0.5])
def test_a_free_or_negative_cost_is_a_no_op(cost):
    """An empty `must_have` email result costs nothing, and callers must not have
    to branch on that to stay honest. True because nothing failed."""
    assert record_spend(cost, kind=KIND_EMAIL) is True
    assert credits_today() == 0.0


# --- limits are projected, not merely compared ------------------------------

def test_a_cost_that_would_cross_the_daily_ceiling_is_refused(credit_ceilings):
    """The shape of the bug this replaces: `spent >= max` was False at 0.9 of
    1.0, so a 0.5 page was authorised and the ceiling overshot by a page."""
    credit_ceilings(day=1.0, month=5.0)
    record_spend(0.9, kind=KIND_DISCOVERY)

    assert credits_today() < 1.0        # the old `spent >= max` test passed here
    assert can_afford(0.05) is True     # 0.95 -> fits
    assert can_afford(0.5) is False     # 1.40 -> refused


def test_exactly_reaching_the_ceiling_is_allowed(credit_ceilings):
    """`>` not `>=`: spending the last credit of the budget is in budget."""
    credit_ceilings(day=1.0, month=5.0)
    record_spend(0.5, kind=KIND_DISCOVERY)
    assert can_afford(0.5) is True


def test_the_monthly_ceiling_refuses_even_when_the_day_has_room(credit_ceilings):
    """The monthly brake sits in front of the vendor's fair-use cap, which resets
    only at subscription renewal and which no retry clears."""
    credit_ceilings(day=1.0, month=5.0)
    log = credit_tracker.load_log()
    log["months"][credit_tracker._month_of(credit_tracker.today_iso())] = 4.9
    credit_tracker._save_log(log)

    assert credits_today() == 0.0       # plenty of daily room
    assert can_afford(0.5) is False     # but the month is nearly gone


def test_a_zero_cost_is_always_affordable(credit_ceilings):
    credit_ceilings(day=1.0, month=5.0)
    record_spend(1.0, kind=KIND_DISCOVERY)
    assert can_afford(0) is True


# --- the vendor's own balance outranks our estimates ------------------------

def test_the_vendor_balance_refuses_what_our_ceilings_would_allow():
    """Our ceilings are calibrated from measured usage; `credits_left` is the
    entitlement. It has to win."""
    record_vendor_balance(0.05)
    assert can_afford(0.5) is False     # both our ceilings are infinite here
    assert can_afford(0.01) is True


def test_the_vendor_balance_persists_for_the_email_step():
    """It arrives only on a DISCOVERY response, so the email step can benefit
    from it only if the ledger carries it across."""
    record_vendor_balance(3.5)
    assert credit_tracker.load_log()["vendor_credits_left"] == 3.5
    record_vendor_balance(2.0)          # a later page overwrites it
    assert credit_tracker.load_log()["vendor_credits_left"] == 2.0


def test_an_absent_vendor_balance_changes_nothing():
    """Unknown must not read as zero-remaining, or the first run against a
    vendor that stops reporting it would refuse everything."""
    record_vendor_balance(None)
    assert credit_tracker.load_log()["vendor_credits_left"] is None
    assert can_afford(5.0) is True


# --- failure direction: never read as zero ---------------------------------

@pytest.mark.parametrize("body", ["{not json at all", "[1, 2, 3]", "null", "12"])
def test_a_corrupt_ledger_raises_at_run_start(corrupt_ledger, body):
    """quota_tracker returns {} here, which reads as "0 spent" and authorises a
    fresh budget. For money that is the one direction that overspends."""
    corrupt_ledger(body)
    with pytest.raises(CreditLedgerUnavailable):
        assert_readable()


def test_a_corrupt_ledger_refuses_spend_mid_run(corrupt_ledger):
    """Mid-run it returns False rather than raising, so one corrupt read stops
    paid calls without unwinding a run that already spent YouTube quota."""
    corrupt_ledger()
    assert can_afford(0.5) is False
    assert record_spend(0.5, kind=KIND_DISCOVERY) is False


def test_a_missing_file_is_not_an_error(monkeypatch, tmp_path):
    """A first run is not a corrupt ledger — an absent file truthfully means
    nothing has been spent."""
    monkeypatch.setattr(credit_tracker, "CREDIT_LOG_FILE", str(tmp_path / "nope.json"))
    assert_readable()
    assert credits_today() == 0.0


# --- the write itself -------------------------------------------------------

def test_the_write_is_atomic_and_leaves_no_tmp_behind():
    import os

    record_spend(0.3, kind=KIND_DISCOVERY)
    assert os.path.exists(credit_tracker.CREDIT_LOG_FILE)
    assert not os.path.exists(f"{credit_tracker.CREDIT_LOG_FILE}.tmp")


def test_the_ledger_is_valid_readable_json():
    record_spend(0.3, kind=KIND_DISCOVERY, detail="a-niche")
    with open(credit_tracker.CREDIT_LOG_FILE, encoding="utf-8") as f:
        log = json.load(f)
    assert set(log) == {"days", "months", "vendor_credits_left"}


def test_it_reuses_quota_trackers_windows_lock_retry():
    """The os.replace PermissionError fix (antivirus/indexer holding the path)
    ended three real runs. A second copy would be a second chance to get it
    wrong."""
    from channel_vetting.budget import quota_tracker

    assert credit_tracker._replace_with_retry is quota_tracker._replace_with_retry


def test_monthly_totals_are_never_pruned():
    """Daily detail is bounded; the monthly record is the only long-run history of
    money spent and costs ~30 bytes a month."""
    log = credit_tracker.load_log()
    log["months"]["2019-01"] = 12.5
    log["days"]["2019-01-01"] = {"total": 12.5, "by_kind": {KIND_EMAIL: 12.5}}
    credit_tracker._save_log(log)

    reloaded = credit_tracker.load_log()
    assert reloaded["months"]["2019-01"] == 12.5
    assert "2019-01-01" not in reloaded["days"]      # far outside retention


# --- the summary line ------------------------------------------------------

def test_the_summary_shows_the_split_both_ceilings_and_the_vendor_figure(credit_ceilings):
    credit_ceilings(day=1.0, month=5.0)
    record_spend(0.5, kind=KIND_DISCOVERY)
    record_spend(0.2, kind=KIND_EMAIL)
    record_vendor_balance(42.0)

    line = spend_summary()
    assert "0.70/1.00" in line
    assert "discovery 0.50" in line and "email 0.20" in line
    assert "0.70/5.00" in line
    assert "vendor reports 42.00 left" in line


def test_the_summary_reports_an_unreadable_ledger_instead_of_raising(corrupt_ledger):
    """It runs inside the run-summary print block; raising there would lose the
    whole summary over a reporting line."""
    corrupt_ledger()
    assert "LEDGER UNAVAILABLE" in spend_summary()
