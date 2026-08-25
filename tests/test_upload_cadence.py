"""
calc_upload_frequency / calc_uploads_per_year turn the sampled upload
timestamps into the cadence signal the pre-push gate reads.

The bug these pin: calc_upload_frequency used a strict
`datetime.strptime(d, "%Y-%m-%dT%H:%M:%SZ")`, which raises ValueError on any
timestamp carrying fractional seconds or a "+00:00" offset instead of a
trailing Z. Nothing between it and run() catches ValueError
(process_candidate -> push_until_full -> run_niche), so a single odd
videoPublishedAt ended the whole run with the quota already spent — the same
unwinding path CLAUDE.md records for the ReadTimeout and os.replace faults.
days_since_last_upload() next door already routed through the tolerant
_parse_iso_timestamp(); this function had drifted from the shared rule.

The second rule here is the direction of the failure. An unreadable timestamp
is ABSENT DATA, so the cadence must come back None (which
pre_push_drop_reason keeps) and never 0.0 (which is below every floor and
discards). Both call sites used to key off the RAW list length, so a sample of
five strings with one readable date counted the four unreadable ones as
evidence of a slow channel.
"""
from datetime import datetime, timedelta, timezone

import pytest


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _spaced(n, gap_days):
    """n timestamps, newest first, `gap_days` apart."""
    now = datetime.now(timezone.utc)
    return [_iso(now - timedelta(days=gap_days * i)) for i in range(n)]


# --- the crash, in each of the shapes that used to raise ---------------------

@pytest.mark.parametrize(
    "odd",
    [
        "2026-08-01T12:00:00.000Z",      # fractional seconds
        "2026-08-01T12:00:00+00:00",     # explicit offset instead of Z
        "2026-08-01",                    # bare date -> the tz-naive coercion
    ],
)
def test_an_odd_timestamp_does_not_raise(odd):
    """Each of these raised ValueError out of process_candidate and killed the
    run. The value must be READ, not merely survived."""
    from channel_vetting.enrichment.channels import calc_upload_frequency

    older = _iso(datetime.now(timezone.utc) - timedelta(days=400))
    freq = calc_upload_frequency([odd, older])
    assert freq > 0


def test_a_mixed_sample_uses_the_readable_dates():
    from channel_vetting.enrichment.channels import calc_upload_frequency

    dates = _spaced(4, 10)
    dates.insert(2, "not-a-timestamp")
    assert calc_upload_frequency(dates) > 0


# --- unmeasurable must be None, never zero ----------------------------------

@pytest.mark.parametrize(
    "dates",
    [
        [],
        ["2026-08-01T12:00:00Z"],                       # one real date
        ["garbage", "also-garbage", "still-garbage"],    # nothing parses
        # Four unreadable strings alongside one date is a ONE-DATE sample. Both
        # call sites used to key off len(upload_dates), which counted the
        # unreadable ones as evidence of a slow channel.
        ["2026-08-01T12:00:00Z", "x", "y", "z", "w"],
    ],
)
def test_too_thin_a_sample_is_unmeasurable_not_zero(dates):
    """None is KEPT by pre_push_drop_reason; 0.0 is below every floor and
    would discard the channel on absent data."""
    from channel_vetting.enrichment.channels import calc_uploads_per_year

    assert calc_uploads_per_year(dates) is None


def test_a_zero_width_window_is_unmeasurable_not_a_number():
    """Every sampled upload on one day cannot support a cadence. This used to
    report float(len(parsed)) — ten same-day uploads claimed 120/yr, under a
    comment saying it could not extrapolate from a zero-width window."""
    from channel_vetting.enrichment.channels import calc_uploads_per_year

    same_day = ["2026-08-01T0%d:00:00Z" % i for i in range(1, 9)]
    assert calc_uploads_per_year(same_day) is None


def test_the_score_facing_float_still_reports_the_zero_width_window():
    """calc_upload_frequency must NOT change here: its float feeds
    calc_overall_score and the "Upload Frequency" text column, so returning
    something new for a same-day window would make every Overall Score already
    in Airtable incomparable with new ones."""
    from channel_vetting.enrichment.channels import calc_upload_frequency

    same_day = ["2026-08-01T0%d:00:00Z" % i for i in range(1, 9)]
    assert calc_upload_frequency(same_day) == 8.0


# --- the conversion has one home --------------------------------------------

def test_annual_is_twelve_times_the_monthly_figure():
    from channel_vetting.enrichment.channels import (
        calc_upload_frequency,
        calc_uploads_per_year,
    )

    dates = _spaced(6, 15)
    assert calc_uploads_per_year(dates) == pytest.approx(
        calc_upload_frequency(dates) * 12
    )


def test_the_pipeline_and_the_audit_script_share_the_conversion():
    """scripts/audit/audit_prospects.py promises it "can never disagree" with the pipeline
    about what fits. Both must reach the same function, not two copies."""
    from channel_vetting import pipeline
    from scripts.audit import audit_prospects
    from channel_vetting.enrichment.channels import calc_uploads_per_year

    assert pipeline.calc_uploads_per_year is calc_uploads_per_year
    assert audit_prospects.calc_uploads_per_year is calc_uploads_per_year


def test_calc_upload_frequency_no_longer_uses_strict_strptime():
    """A regression guard on the mechanism, not just the symptom: re-adding a
    strict strptime here would pass every test above that happens to use a
    Z-suffixed date."""
    import inspect

    from channel_vetting.enrichment.channels import calc_upload_frequency

    src = inspect.getsource(calc_upload_frequency)
    # The CALL form, not the bare word: the comments name strptime deliberately
    # to explain why it is gone, and only a call can reintroduce the bug.
    assert "strptime(" not in src
