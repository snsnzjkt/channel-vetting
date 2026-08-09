"""Per-niche qualification thresholds, from the April 2024 briefs."""
import pytest

HOME_THEATER_MIN_VIEWS = 10_000
LIFESTYLE_MIN_VIEWS = 2_000


def test_meets_both_criteria():
    from scoring import QUALIFIED, qualify

    assert qualify(15_000, 24, HOME_THEATER_MIN_VIEWS, 12) == QUALIFIED


def test_exactly_at_view_minimum_qualifies():
    from scoring import QUALIFIED, qualify

    assert qualify(10_000, 24, HOME_THEATER_MIN_VIEWS, 12) == QUALIFIED


def test_exactly_at_age_minimum_qualifies():
    """Deferred boundary case: a channel exactly at the minimum age (not
    a day younger) must qualify, not be flagged as NEW_CHANNEL."""
    from scoring import QUALIFIED, qualify

    assert qualify(15_000, 12, 10_000, 12) == QUALIFIED


def test_just_below_view_minimum_is_flagged():
    from scoring import BELOW_VIEW_MINIMUM, qualify

    assert qualify(9_999, 24, HOME_THEATER_MIN_VIEWS, 12) == BELOW_VIEW_MINIMUM


def test_lifestyle_uses_its_own_lower_threshold():
    """2,500 views fails Home Theater but passes Lifestyle."""
    from scoring import BELOW_VIEW_MINIMUM, QUALIFIED, qualify

    assert qualify(2_500, 24, HOME_THEATER_MIN_VIEWS, 12) == BELOW_VIEW_MINIMUM
    assert qualify(2_500, 24, LIFESTYLE_MIN_VIEWS, None) == QUALIFIED


def test_young_channel_is_flagged():
    from scoring import NEW_CHANNEL, qualify

    assert qualify(15_000, 6, HOME_THEATER_MIN_VIEWS, 12) == NEW_CHANNEL


def test_view_failure_wins_when_both_fail():
    from scoring import BELOW_VIEW_MINIMUM, qualify

    assert qualify(500, 3, HOME_THEATER_MIN_VIEWS, 12) == BELOW_VIEW_MINIMUM


def test_unknown_age_does_not_disqualify():
    from scoring import QUALIFIED, qualify

    assert qualify(15_000, None, HOME_THEATER_MIN_VIEWS, 12) == QUALIFIED


def test_no_age_requirement_ignores_young_channel():
    from scoring import QUALIFIED, qualify

    assert qualify(5_000, 1, LIFESTYLE_MIN_VIEWS, None) == QUALIFIED


@pytest.mark.parametrize(
    "niche,expected_views,expected_age",
    [("Home Theater", 10_000, 12), ("Lifestyle Sofa", 2_000, None)],
)
def test_niche_criteria_match_the_briefs(niche, expected_views, expected_age):
    from main import NICHES

    assert NICHES[niche]["min_avg_views"] == expected_views
    assert NICHES[niche]["min_channel_age_months"] == expected_age


def test_qualification_literals_match_the_airtable_options():
    """
    "New Channel" appears exactly once in the whole repo — its own
    definition in scoring.py — and every other assertion in this file
    compares qualify()'s output against the same constant qualify()
    returns, which is tautological. A rename would keep every one of
    those tests green while push_record's typecast=True silently mints a
    fourth Airtable "Qualification" option, and rows stop matching the
    human review view. This pins the literal strings against the live
    Airtable options (Qualified / Below View Minimum / New Channel) so a
    rename fails loudly here instead.
    """
    from scoring import QUALIFIED, BELOW_VIEW_MINIMUM, NEW_CHANNEL

    assert (QUALIFIED, BELOW_VIEW_MINIMUM, NEW_CHANNEL) == (
        "Qualified", "Below View Minimum", "New Channel")
