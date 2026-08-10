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


# --- 2026-08 criteria change ---------------------------------------------
#
# Qualify if avg views >= 2,000 AND (avg views / subscribers) >= 0.05.
# 1,000 subscribers is a SOFT floor: below it, Engagement Rate >= 1.5%
# has to vouch for the channel. The 2,000 floor STACKS with each niche's
# own minimum rather than replacing it, so Home Theater still wants 10,000.

HOME_THEATER_AGE = 12


def test_global_view_floor_stacks_on_top_of_a_lower_niche_minimum():
    """A niche minimum below 2,000 can no longer let a channel through."""
    from scoring import BELOW_VIEW_MINIMUM, QUALIFIED, qualify

    assert qualify(1_999, 24, 500, None, subscriber_count=10_000, engagement_rate=5.0) == BELOW_VIEW_MINIMUM
    assert qualify(2_000, 24, 500, None, subscriber_count=10_000, engagement_rate=5.0) == QUALIFIED


def test_home_theater_keeps_its_own_higher_minimum():
    """Stacking must not lower the 10,000 bar from the brief."""
    from scoring import BELOW_VIEW_MINIMUM, qualify

    assert qualify(9_999, 24, HOME_THEATER_MIN_VIEWS, HOME_THEATER_AGE,
                   subscriber_count=20_000, engagement_rate=5.0) == BELOW_VIEW_MINIMUM


def test_view_to_sub_ratio_below_the_floor_is_flagged():
    from scoring import BELOW_VIEW_MINIMUM, qualify

    # 13,006 views against 274,000 subs = 0.047 — the live Budget Gadget
    # Tamil row, the only currently-Qualified channel this rule demotes.
    assert qualify(13_006.5, 24, HOME_THEATER_MIN_VIEWS, HOME_THEATER_AGE,
                   subscriber_count=274_000, engagement_rate=3.13) == BELOW_VIEW_MINIMUM


def test_view_to_sub_ratio_exactly_at_the_floor_qualifies():
    from scoring import QUALIFIED, qualify

    assert qualify(5_000, 24, 2_000, None,
                   subscriber_count=100_000, engagement_rate=3.0) == QUALIFIED


def test_unknown_subscriber_count_does_not_disqualify():
    """Absent data is not evidence against a channel (same rule as age)."""
    from scoring import QUALIFIED, qualify

    assert qualify(15_000, 24, HOME_THEATER_MIN_VIEWS, HOME_THEATER_AGE,
                   subscriber_count=None, engagement_rate=None) == QUALIFIED


def test_below_the_sub_soft_floor_low_engagement_is_flagged():
    from scoring import BELOW_VIEW_MINIMUM, qualify

    assert qualify(15_000, 24, HOME_THEATER_MIN_VIEWS, HOME_THEATER_AGE,
                   subscriber_count=900, engagement_rate=1.4) == BELOW_VIEW_MINIMUM


def test_below_the_sub_soft_floor_good_engagement_qualifies():
    """This is what makes 1,000 a SOFT floor rather than a hard one."""
    from scoring import QUALIFIED, qualify

    assert qualify(15_000, 24, HOME_THEATER_MIN_VIEWS, HOME_THEATER_AGE,
                   subscriber_count=900, engagement_rate=1.5) == QUALIFIED


def test_above_the_sub_soft_floor_low_engagement_still_qualifies():
    """The 1.5% rule resolves borderline cases only — it is not a global AND."""
    from scoring import QUALIFIED, qualify

    assert qualify(15_000, 24, HOME_THEATER_MIN_VIEWS, HOME_THEATER_AGE,
                   subscriber_count=50_000, engagement_rate=0.9) == QUALIFIED


def test_view_failure_still_outranks_a_young_channel():
    from scoring import BELOW_VIEW_MINIMUM, qualify

    assert qualify(500, 3, HOME_THEATER_MIN_VIEWS, HOME_THEATER_AGE,
                   subscriber_count=80_000, engagement_rate=4.0) == BELOW_VIEW_MINIMUM


def test_every_live_qualified_row_except_one_survives_the_new_rules():
    """
    Regression against the real table: of the 12 Qualified Home Theater
    rows, only Budget Gadget Tamil (ratio 0.047) is demoted.
    """
    from scoring import BELOW_VIEW_MINIMUM, QUALIFIED, qualify

    # (name, avg_views, subs, engagement_rate)
    live = [
        ("Retro Media Crypt", 16_159.8, 2_400, 4.14),
        ("Amazing World Bike Tour", 10_456, 12_400, 7.13),
        ("Theater At Home", 18_444.2, 27_200, 3.73),
        ("Hackshop Garage", 18_575.4, 49_600, 5.51),
        ("iiWi Reviews", 12_433.4, 59_600, 5.18),
        ("Will and Mary Outdoors", 594_872.7, 116_000, 1.0),
        ("Kat Viana", 28_714.6, 266_000, 8.56),
        ("Technical Chennai", 165_202.1, 387_000, 2.09),
        ("Amazing Anime Man", 42_406.5, 426_000, 2.37),
        ("Andrew Robinson", 62_982.5, 440_000, 3.6),
        ("Chris Koerner", 54_806.5, 658_000, 3.0),
    ]
    for name, views, subs, er in live:
        got = qualify(views, 24, HOME_THEATER_MIN_VIEWS, HOME_THEATER_AGE,
                      subscriber_count=subs, engagement_rate=er)
        assert got == QUALIFIED, f"{name} should still qualify, got {got}"

    demoted = qualify(13_006.5, 24, HOME_THEATER_MIN_VIEWS, HOME_THEATER_AGE,
                      subscriber_count=274_000, engagement_rate=3.13)
    assert demoted == BELOW_VIEW_MINIMUM


def test_new_rules_reuse_the_three_existing_airtable_options():
    """
    push_record sends typecast=True, which SILENTLY mints a new Single
    Select option rather than erroring. The ratio and soft-floor failures
    therefore reuse "Below View Minimum" instead of inventing a fourth
    value and mutating the live table's schema behind the reviewer's back.
    """
    from scoring import QUALIFIED, BELOW_VIEW_MINIMUM, NEW_CHANNEL
    import scoring

    assert {QUALIFIED, BELOW_VIEW_MINIMUM, NEW_CHANNEL} == {
        v for k, v in vars(scoring).items()
        if k.isupper() and isinstance(v, str) and not k.startswith("_")
    }
