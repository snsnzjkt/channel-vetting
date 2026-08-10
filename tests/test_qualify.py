"""
What is left of qualify() after the 2026-08 criteria change: channel age,
and nothing else.

The view floor, the video-count floor and the search zone all became HARD
requirements in the same change — a channel that misses one is discarded
at main.pre_push_drop_reason() and no row is written, so qualify() has
nothing to say about it. See tests/test_prepush_gate.py for those.

That leaves two outcomes where there were three. "Below View Minimum" is
gone from scoring.py entirely (its Airtable option stays, holding the rows
written under the old rules), and a young-but-real channel is still worth a
human's attention, which is what NEW_CHANNEL is for.
"""
import pytest

HOME_THEATER_AGE = 12


def test_a_channel_past_the_age_minimum_qualifies():
    from scoring import QUALIFIED, qualify

    assert qualify(24, HOME_THEATER_AGE) == QUALIFIED


def test_exactly_at_age_minimum_qualifies():
    """Boundary: exactly at the minimum age (not a day younger) qualifies."""
    from scoring import QUALIFIED, qualify

    assert qualify(12, HOME_THEATER_AGE) == QUALIFIED


def test_young_channel_is_flagged():
    from scoring import NEW_CHANNEL, qualify

    assert qualify(6, HOME_THEATER_AGE) == NEW_CHANNEL


def test_unknown_age_does_not_disqualify():
    """Absent data is not evidence against a channel."""
    from scoring import QUALIFIED, qualify

    assert qualify(None, HOME_THEATER_AGE) == QUALIFIED


def test_no_age_requirement_ignores_a_young_channel():
    """Lifestyle Sofa's brief sets no age bar, so min is None."""
    from scoring import QUALIFIED, qualify

    assert qualify(1, None) == QUALIFIED


def test_qualify_no_longer_takes_view_arguments():
    """
    The old signature was qualify(avg_views, age, min_avg_views,
    min_age, ...). A call site left on that shape would now pass avg_views
    where channel_age_months belongs and silently compare a view count
    against a month count — every channel with more than 12 average views
    would come back Qualified regardless of its age.
    """
    import inspect

    from scoring import qualify

    assert list(inspect.signature(qualify).parameters) == [
        "channel_age_months",
        "min_channel_age_months",
    ]


# --- what the niches ask for ---------------------------------------------


@pytest.mark.parametrize(
    "niche,expected_views,expected_age",
    [("Home Theater", 10_000, 12), ("Lifestyle Sofa", 10_000, None)],
)
def test_both_niches_share_the_same_view_floor(niche, expected_views, expected_age):
    """
    2026-08: both niches want 10,000 average views. Lifestyle Sofa was
    raised from the 2,000 in its brief; Home Theater is unchanged. The age
    requirement stayed per-niche and is NOT unified.
    """
    from main import NICHES

    assert NICHES[niche]["min_avg_views"] == expected_views
    assert NICHES[niche]["min_channel_age_months"] == expected_age


def test_qualification_literals_match_the_airtable_options():
    """
    Every other assertion in this file compares qualify()'s output against
    the same constant qualify() returns, which is tautological. A rename
    would keep all of them green while push_record's typecast=True silently
    mints a NEW Airtable "Qualification" option, and rows stop matching the
    reviewer's saved views. This pins the literal strings.
    """
    from scoring import QUALIFIED, NEW_CHANNEL

    assert (QUALIFIED, NEW_CHANNEL) == ("Qualified", "New Channel")


def test_scoring_exposes_exactly_two_qualification_values():
    """
    push_record sends typecast=True, which SILENTLY creates a missing
    Single Select option rather than erroring. A third value added here
    would mutate the live table's schema behind the reviewer's back, so
    adding one has to be a deliberate act that fails this test first.
    """
    import scoring
    from scoring import QUALIFIED, NEW_CHANNEL

    assert {QUALIFIED, NEW_CHANNEL} == {
        v for k, v in vars(scoring).items()
        if k.isupper() and isinstance(v, str) and not k.startswith("_")
    }


def test_below_view_minimum_is_gone_from_scoring():
    """
    Its removal is the point of the change — a channel under the view floor
    is now discarded rather than written as a flag. A re-added constant
    here would mean the flag path came back without the gate being removed.
    """
    import scoring

    assert not hasattr(scoring, "BELOW_VIEW_MINIMUM")
