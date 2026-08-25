"""Channel age derives from publishedAt; absent data must not disqualify."""
import pytest


def test_age_of_known_date():
    from channel_vetting.enrichment.channels import channel_age_months

    age = channel_age_months("2024-08-07T00:00:00Z")
    assert age is not None and age > 12


def test_recent_channel_is_young():
    from datetime import datetime, timedelta, timezone

    from channel_vetting.enrichment.channels import channel_age_months

    recent = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    age = channel_age_months(recent)
    assert age is not None and age < 2


@pytest.mark.parametrize("bad", ["", None, "not-a-date"])
def test_unparseable_returns_none(bad):
    """None means 'unknown', and unknown must never be treated as new."""
    from channel_vetting.enrichment.channels import channel_age_months

    assert channel_age_months(bad) is None


def test_a_bare_date_publishedat_is_handled_not_crashed():
    """A date-only publishedAt parses tz-naive; age must compute without
    raising a TypeError against an aware 'now'."""
    from channel_vetting.enrichment.channels import channel_age_months

    age = channel_age_months("2020-01-01")
    assert age is not None and age > 12
