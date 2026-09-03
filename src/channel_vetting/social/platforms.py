"""
One registry for everything that differs between platforms.

WHY THIS EXISTS. Before it, adding a third platform meant finding and editing
per-platform branches in SIX modules — criteria.py held the engagement floors
and the denominator rule, pipeline.py held the destination table and four
`is_tiktok` ternaries choosing column names, handles.py held the profile-URL
template, posts.py held the page size, discovery.py held the supported list.
Each of those is a place to forget. A forgotten one does not raise: it silently
falls into the TikTok branch, so a new platform would be judged on TikTok's
floors and written to TikTok's columns.

Now every one of those lives in `PLATFORMS` below and the modules read it. To
add a platform: append an entry, add the two prospect-table env vars, and build
the Airtable table with the column names named here. Nothing else branches.

WHAT IS DELIBERATELY *NOT* HERE. The lane definitions (`lanes.py`), the pet
requirement (`relevance.py`) and the criteria thresholds' *values* (config) are
business rules that happen to be platform-agnostic today. Pulling them in would
imply they vary per platform when they do not.
"""

TIKTOK = "tiktok"
INSTAGRAM = "instagram"

# Engagement denominators. The criteria draft: "TikTok and Instagram can't share
# a number. TikTok pushes most content to non-followers, so engagement is
# measured per view. Instagram is still follower-led, so per follower."
DENOM_VIEWS = "views"
DENOM_FOLLOWERS = "followers"

PLATFORMS = {
    TIKTOK: {
        "label": "TikTok",
        # Which config attribute names the destination table. Read by name so
        # config stays a flat module and this file needs no import of it.
        "table_config_attr": "AIRTABLE_TABLE_TIKTOK_PROSPECTS",
        "min_median_views_attr": "SOCIAL_TIKTOK_MIN_MEDIAN_VIEWS",
        "denominator": DENOM_VIEWS,
        # Engagement floors by follower band, as FRACTIONS. The draft gives
        # TikTok three tiers and Instagram four, which is why micro shares mid's
        # rate here. That asymmetry is the draft's, not a transcription slip.
        "engagement_floors": {
            "small": 0.040,
            "micro": 0.030,
            "mid": 0.030,
            "big": 0.025,
        },
        "profile_url": "https://www.tiktok.com/@{handle}",
        # Requested posts per page. Instagram ignores it (fixed 12); TikTok caps
        # at 35. The price is per REQUEST, not per post, so asking for more is
        # free and gives the cadence figure more spine.
        "page_size": 30,
        "asset_set": "Mythumi — TikTok",
        # Airtable column names. The engagement column is NAMED for its
        # denominator so the two platforms' numbers can never be compared by
        # accident, and so a wrong write is a 422 rather than a silent lie.
        "engagement_column": "Engagement Rate (per view)",
        "reach_mean_column": "Avg Views per Post",
        # Neither platform returns shares today; the column exists on TikTok
        # only, and posts.py leaves it blank rather than writing a false zero.
        "shares_column": "Avg Shares per Post",
        # Audience age is available for Instagram 10k+ from discovery and has no
        # TikTok source at all, so the reviewer note differs.
        "audience_age_available": False,
    },
    INSTAGRAM: {
        "label": "Instagram",
        "table_config_attr": "AIRTABLE_TABLE_INSTAGRAM_PROSPECTS",
        "min_median_views_attr": "SOCIAL_INSTAGRAM_MIN_MEDIAN_VIEWS",
        "denominator": DENOM_FOLLOWERS,
        # Lower-looking numbers are the HARSHER test: "under 1% on Instagram
        # almost always means bought followers."
        "engagement_floors": {
            "small": 0.030,
            "micro": 0.020,
            "mid": 0.015,
            "big": 0.010,
        },
        "profile_url": "https://www.instagram.com/{handle}/",
        "page_size": 12,
        "asset_set": "Mythumi — Instagram",
        "engagement_column": "Engagement Rate (per follower)",
        # Instagram's "views" ARE Reel plays; static posts report none, which is
        # why the median drops unmeasured posts instead of scoring them zero.
        "reach_mean_column": "Avg Reel Plays",
        "shares_column": None,
        "audience_age_available": True,
    },
}

SUPPORTED = tuple(PLATFORMS)


def spec(platform: str) -> dict:
    """
    The registry entry for `platform`, or raise.

    RAISES rather than defaulting. A silent default is how a new platform gets
    judged on TikTok's floors and written to TikTok's columns — the exact
    failure this module exists to make impossible.
    """
    key = (platform or "").lower()
    try:
        return PLATFORMS[key]
    except KeyError:
        raise ValueError(
            f"unsupported social platform {platform!r} — add an entry to "
            f"social.platforms.PLATFORMS. Known: {', '.join(SUPPORTED)}"
        ) from None


def label(platform: str) -> str:
    """The human/Airtable spelling, e.g. "TikTok"."""
    return spec(platform)["label"]


def denominator(platform: str) -> str:
    """"views" or "followers" — what engagement is measured against."""
    return spec(platform)["denominator"]
