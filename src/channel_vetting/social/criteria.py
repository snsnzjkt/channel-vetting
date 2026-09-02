"""
The creator criteria draft, as code. TikTok and Instagram only.

Source: "Creator criteria - first draft", 2026-09-03. Two of its statements
shape this whole module and are worth quoting, because they are the reason it
does not look like the YouTube gates:

  "TikTok and Instagram can't share a number. TikTok pushes most content to
   non-followers, so engagement is measured per view. Instagram is still
   follower-led, so per follower."

  "Judge on the median of the last 10 posts, not the average - one viral video
   shouldn't carry someone through."

So there is no single engagement number and no mean anywhere in here. Both
would be quietly wrong rather than loudly wrong.

WHAT THIS MODULE DELIBERATELY DOES NOT DO: produce a score out of 100. The
draft's rubric is 100 points across eleven components, and only three of them
can be computed from data we can buy (engagement above the floor, audience in
US/CA, saves and shares). The rest - comment quality, reveal content, whether
the subject is filmable, whether they already tag products, organic growth,
over-sponsorship, ad disclosure - need a person looking at the account. A
number that summed only the automatable third and still called itself "out of
100, pass at 65" would read as authoritative while being mostly blank, which is
the same trap `_to_candidate()` avoids by refusing to carry vendor statistics.
`auto_score()` therefore returns its own subtotal AND its own maximum, so the
caller can never mistake it for the finished rubric.
"""
import logging
import os
import statistics

from channel_vetting import config

logger = logging.getLogger(__name__)

PLATFORM_TIKTOK = "tiktok"
PLATFORM_INSTAGRAM = "instagram"
SOCIAL_PLATFORMS = (PLATFORM_TIKTOK, PLATFORM_INSTAGRAM)

# Follower bands. THE BOUNDARIES ARE AN ASSUMPTION, NOT FROM THE DRAFT: it names
# small / micro / mid / big tiers and gives a rate for each, but never says
# where one ends. These are the conventional creator-economy bands, and the one
# figure the draft DOES pin is consistent with them - "Micro, 10K-100K
# followers" is called out as the band to actually target. Change them here if
# the draft's owner means something different; every threshold below reads from
# this one table.
BAND_SMALL = "small"
BAND_MICRO = "micro"
BAND_MID = "mid"
BAND_BIG = "big"

_BAND_CEILINGS = (
    (10_000, BAND_SMALL),
    (100_000, BAND_MICRO),
    (500_000, BAND_MID),
)


def follower_band(followers: int) -> str:
    """Which size tier a follower count falls in."""
    for ceiling, band in _BAND_CEILINGS:
        if (followers or 0) < ceiling:
            return band
    return BAND_BIG


# Engagement floors, as FRACTIONS.
#
# TikTok is measured per VIEW; Instagram per FOLLOWER. The draft gives TikTok
# three tiers and Instagram four, which is why TikTok's table has no separate
# micro entry - micro shares mid's 3%. That asymmetry is the draft's, not a
# transcription slip.
#
# The draft's own note on why Instagram's lower-looking numbers are the harsher
# test: "under 1% on Instagram almost always means bought followers."
_ENGAGEMENT_FLOORS = {
    PLATFORM_TIKTOK: {
        BAND_SMALL: 0.040,
        BAND_MICRO: 0.030,
        BAND_MID: 0.030,
        BAND_BIG: 0.025,
    },
    PLATFORM_INSTAGRAM: {
        BAND_SMALL: 0.030,
        BAND_MICRO: 0.020,
        BAND_MID: 0.015,
        BAND_BIG: 0.010,
    },
}

# "Who to actually go after: Micro, 10K-100K followers, above 3.5%
# engagement. That band drives ~29% of TikTok affiliate sales."
# A PRIORITY signal, never a rejection.
PRIORITY_BAND = BAND_MICRO
PRIORITY_MIN_ENGAGEMENT = 0.035


def engagement_floor(platform: str, followers: int) -> float:
    """The minimum engagement fraction for this platform and follower band."""
    table = _ENGAGEMENT_FLOORS.get((platform or "").lower())
    if not table:
        raise ValueError(f"no engagement floor table for platform {platform!r}")
    return table[follower_band(followers)]


def min_median_views(platform: str) -> int:
    """The median-reach floor. Separate per platform, per the draft."""
    p = (platform or "").lower()
    if p == PLATFORM_TIKTOK:
        return config.SOCIAL_TIKTOK_MIN_MEDIAN_VIEWS
    if p == PLATFORM_INSTAGRAM:
        return config.SOCIAL_INSTAGRAM_MIN_MEDIAN_VIEWS
    raise ValueError(f"no median-views floor for platform {platform!r}")


def median_views(view_counts) -> int | None:
    """
    Median of the supplied per-post view counts, or None if there are none.

    MEDIAN, not mean. Posts with a missing/None view count are dropped rather
    than counted as zero: Instagram reports plays for Reels but not for static
    posts, and treating an unmeasured post as a zero-view post would drag the
    median of a perfectly healthy account below the floor.
    """
    usable = [int(v) for v in (view_counts or []) if isinstance(v, (int, float)) and v >= 0]
    if not usable:
        return None
    return int(statistics.median(usable))


def engagement_rate(platform: str, *, interactions: int, views: int, followers: int):
    """
    Engagement as a FRACTION, computed the way the platform requires.

    TikTok divides by VIEWS, Instagram by FOLLOWERS. Returns None when the
    denominator is missing or zero — an unknown rate must not be mistaken for a
    zero rate, because zero fails every floor and would silently reject the
    account instead of flagging it as unmeasured.
    """
    p = (platform or "").lower()
    if p == PLATFORM_TIKTOK:
        denominator = views
    elif p == PLATFORM_INSTAGRAM:
        denominator = followers
    else:
        raise ValueError(f"no engagement rule for platform {platform!r}")
    if not denominator or denominator <= 0:
        return None
    return (interactions or 0) / denominator


# --- The auto-reject list -------------------------------------------------
#
# The draft's table, in its own order. Each returns a short machine-readable
# reason so the run summary can say WHICH gate a creator died on, the way
# pre_push_drop_reason does for YouTube.
#
# NOT IMPLEMENTED HERE, AND THAT IS THE POINT — the four checks no purchasable
# data answers:
#   - "Has a usable subject"      -> is there a recurring pet/person to model
#   - "Photo quality good enough" -> the draft calls this "specific to us, and
#                                    it's a gate"
#   - "Fake follower risk"        -> the draft prescribes manual checks
#   - "Audience 18+ 70%+"         -> available for Instagram 10k+ from the
#                                    vendor, absent for TikTok
# These are left to the reviewer, which is why every admitted row lands as
# Review Decision = Pending rather than Qualified.
REASON_FOLLOWERS = "below_follower_minimum"
REASON_MEDIAN_VIEWS = "below_median_reach"
REASON_ENGAGEMENT = "below_engagement_floor"
REASON_STALE = "no_recent_post"
REASON_CADENCE = "posts_too_infrequently"
REASON_COUNTRY = "outside_search_zone"
REASON_UNMEASURED = "unmeasured_no_post_data"


def auto_reject_reason(platform: str, *, followers, metrics, country=None) -> str | None:
    """
    The first auto-reject rule this creator fails, or None if they clear all of
    the ones that can be checked from data.

    `metrics` is a PostMetrics from social.posts. A creator with NO post data is
    rejected as `unmeasured_no_post_data` rather than passed through: every
    remaining gate depends on it, so admitting them would mean admitting a row
    screened on follower count alone. That is the degradation the posts budget
    exists to prevent — see SOCIAL_MIN_POSTS_SCREENS_PER_RUN.
    """
    followers = int(followers or 0)
    if followers < config.SOCIAL_MIN_FOLLOWERS:
        return REASON_FOLLOWERS

    if country and config.SOCIAL_ALLOWED_COUNTRIES:
        if str(country).strip().upper() not in config.SOCIAL_ALLOWED_COUNTRIES:
            return REASON_COUNTRY

    if metrics is None or not metrics.measured:
        return REASON_UNMEASURED

    if metrics.median_views is None or metrics.median_views < min_median_views(platform):
        return REASON_MEDIAN_VIEWS

    rate = metrics.engagement_rate(platform, followers)
    if rate is None or rate < engagement_floor(platform, followers):
        return REASON_ENGAGEMENT

    if metrics.days_since_last_post is None:
        return REASON_UNMEASURED
    if metrics.days_since_last_post > config.SOCIAL_MAX_DAYS_SINCE_POST:
        return REASON_STALE

    if metrics.posts_per_week is None:
        return REASON_UNMEASURED
    if metrics.posts_per_week < config.SOCIAL_MIN_POSTS_PER_WEEK:
        return REASON_CADENCE

    return None


# --- The automatable slice of the 100-point rubric ------------------------
#
# Component weights are the draft's. `automatable` records whether we can
# compute it, and it is False for most of them on purpose.
SCORE_COMPONENTS = (
    ("engagement_above_floor", 15, True),
    ("comment_quality", 12, False),   # "Read 30 comments, rate them out of 10"
    ("audience_in_us_ca", 12, True),  # discovery location filter
    ("does_reveal_content", 12, False),
    ("subject_clearly_filmable", 10, False),
    ("already_tags_products", 10, False),
    ("saves_and_shares", 8, True),    # shares present in TikTok post data
    ("growth_looks_organic", 7, False),
    ("not_over_sponsored", 6, False),
    ("discloses_ads_properly", 5, False),
    ("on_both_platforms", 3, False),
)

AUTO_SCORE_MAX = sum(w for _, w, auto in SCORE_COMPONENTS if auto)
RUBRIC_MAX = sum(w for _, w, _ in SCORE_COMPONENTS)
# "Pass at 65." Unreachable from purchasable data alone (AUTO_SCORE_MAX is 35),
# which is the honest state of affairs — a reviewer supplies the rest.
RUBRIC_PASS_MARK = int(os.getenv("SOCIAL_RUBRIC_PASS_MARK", 65))


def auto_score(platform: str, *, followers, metrics, country=None) -> tuple[int, int]:
    """
    (points, max_points) for ONLY the rubric components we can compute.

    Returns its own maximum alongside the subtotal so a caller cannot present
    it as the draft's score out of 100 by accident. AUTO_SCORE_MAX is 35 of
    RUBRIC_MAX 100; the pass mark of 65 is therefore unreachable from data
    alone, which is the honest state of affairs and not a bug to tune away.
    """
    points = 0
    if metrics is not None and metrics.measured:
        rate = metrics.engagement_rate(platform, followers)
        floor = engagement_floor(platform, followers)
        if rate is not None and rate >= floor:
            points += 15
            # A full-marks bonus is NOT awarded for clearing the floor by a
            # wide margin: the draft gives the component 15 points for being
            # "above the floor", full stop.
        if metrics.total_shares:
            points += 8
    if country and str(country).strip().upper() in (config.SOCIAL_ALLOWED_COUNTRIES or ()):
        points += 12
    return points, AUTO_SCORE_MAX


def is_priority(platform: str, *, followers, metrics) -> bool:
    """
    Whether this creator is in the band the draft says to chase: micro
    (10K-100K) above 3.5% engagement.
    """
    if metrics is None or not metrics.measured:
        return False
    if follower_band(int(followers or 0)) != PRIORITY_BAND:
        return False
    rate = metrics.engagement_rate(platform, followers)
    return rate is not None and rate >= PRIORITY_MIN_ENGAGEMENT
