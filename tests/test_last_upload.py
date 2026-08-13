"""
days_since_last_upload turns the sampled upload timestamps into a channel-
activity signal for the "last upload within a rolling 12 months" gate.

Two rules mirror channel_age_months: the NEWEST sampled upload is the one
that counts (a channel that just posted after a long gap is active), and
absent/unparseable data returns None — unknown must never read as stale.
"""
from datetime import datetime, timedelta, timezone

import pytest


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_days_since_a_recent_upload_is_small():
    from enrichment import days_since_last_upload

    recent = _iso(datetime.now(timezone.utc) - timedelta(days=10))
    days = days_since_last_upload([recent])
    assert days is not None and 9 <= days <= 11


def test_a_stale_channel_is_over_a_year():
    from enrichment import days_since_last_upload

    old = _iso(datetime.now(timezone.utc) - timedelta(days=500))
    days = days_since_last_upload([old])
    assert days is not None and days > 365


def test_the_newest_sampled_upload_is_the_one_that_counts():
    """A long gap followed by a fresh upload is an ACTIVE channel — the
    result must track the newest date, and must not depend on list order."""
    from enrichment import days_since_last_upload

    old = _iso(datetime.now(timezone.utc) - timedelta(days=800))
    fresh = _iso(datetime.now(timezone.utc) - timedelta(days=5))

    days = days_since_last_upload([old, fresh])
    assert days is not None and days < 10
    assert days_since_last_upload([fresh, old]) == days


@pytest.mark.parametrize("dates", [[], ["", None], ["not-a-date"], [None], ["garbage", ""]])
def test_unknown_or_unparseable_returns_none(dates):
    from enrichment import days_since_last_upload

    assert days_since_last_upload(dates) is None
