"""
Budgeted TikTok / Instagram discovery.

Reuses `InfluencerDiscovery` rather than reimplementing it, so pagination, the
server-side exclusion set, the credit ceilings, the fair-use handle meter and
the fail-soft contract are all the SAME code the YouTube path runs. The two
things that had to differ are injected rather than forked:

  1. the `platform` (the vendor's required discriminator), and
  2. the handle normaliser, because normalize_handle() returns "" for a bare
     username and would have discarded every Instagram creator silently.

FILTERS ARE BUILT HERE, NOT TAKEN FROM `NICHES`. The vendor's filter fields
change per platform, and the YouTube niches carry YouTube-shaped criteria
(min_avg_views, channel age, long-form counts) with no TikTok/Instagram meaning.

THE SERVER-SIDE ENGAGEMENT FLOOR IS DELIBERATELY THE MOST PERMISSIVE ONE.
The criteria draft's floors vary by follower band — TikTok 4%/3%/2.5%,
Instagram 3%/2%/1.5%/1% — and a single server-side number cannot express that.
Sending the STRICTEST would silently exclude large accounts that legitimately
sit at the bottom of the scale (a 600k Instagram creator at 1.2% clears its own
band but fails a 3% filter). So the filter sends the LOWEST floor for the
platform, buys a slightly wider pool, and `criteria.auto_reject_reason` applies
the exact per-band floor locally. Erring the other way would lose real
prospects permanently, and this only costs a few 0.01s.
"""
import logging

from channel_vetting import config
from channel_vetting.discovery.search_zones import ZONE_CORE, vendor_locations_for
from channel_vetting.discovery.influencers_club import (
    DEFAULT_SORT,
    InfluencerDiscovery,
    PLATFORM_INSTAGRAM,
    PLATFORM_TIKTOK,
)
from channel_vetting.social.criteria import _ENGAGEMENT_FLOORS
from channel_vetting.social.handles import normalize_social_handle

logger = logging.getLogger(__name__)

SUPPORTED = (PLATFORM_TIKTOK, PLATFORM_INSTAGRAM)


def most_permissive_engagement_percent(platform: str) -> float:
    """
    The lowest band floor for `platform`, as a PERCENT (the vendor's unit).

    Derived from the criteria tables rather than restated, so retuning a floor
    in one place cannot leave this filter stricter than the gates it feeds.
    """
    floors = _ENGAGEMENT_FLOORS[(platform or "").lower()]
    return round(min(floors.values()) * 100, 4)


def build_filters(platform: str, lane: dict | None = None) -> dict:
    """
    The discovery filter body for one platform and one sourcing lane.

    Only fields the vendor documents are sent. `location` is included ONLY when
    SOCIAL_SEND_LOCATION_FILTER is on — see the config note on why an
    unverified country format is a silently-empty run rather than an error.
    """
    platform = (platform or "").lower()
    if platform not in SUPPORTED:
        raise ValueError(f"unsupported social platform {platform!r}")

    filters = {
        "number_of_followers": {"min": int(config.SOCIAL_MIN_FOLLOWERS)},
        "engagement_percent": {"min": most_permissive_engagement_percent(platform)},
    }

    # The SAME zone the Valencia niches send, via the same all-or-nothing
    # helper: [] rather than a lossy subset, so a missing country name leaves
    # the filter off instead of silently excluding creators the zone allows.
    locations = vendor_locations_for(ZONE_CORE)
    if locations:
        filters["location"] = locations
    else:
        logger.warning(
            "no verified vendor location names for the social zone — running "
            "WITHOUT a location filter, so out-of-zone creators will be billed "
            "and must be rejected by a reviewer"
        )

    # RELEVANCE. `ai_search` is a documented FILTER field and measured highly
    # selective (11.1M -> 39k on its own). Deliberately NOT
    # `keywords_not_in_description`, which both platforms ACCEPT and silently
    # ignore — probed 2026-09-03, identical result totals with and without it.
    # Artist exclusion therefore happens locally, in social/relevance.py.
    query = (lane or {}).get("ai_search")
    if query:
        filters["ai_search"] = query

    return filters


def client_for_run(*, max_credits=None, enabled=True) -> InfluencerDiscovery:
    """
    A discovery client scoped to the SOCIAL per-run credit ceiling.

    Defaults to SOCIAL_MAX_DISCOVERY_CREDITS_PER_RUN rather than the YouTube
    path's INFLUENCERS_MAX_DISCOVERY_CREDITS_PER_RUN, because the two runs buy
    different things — but both still answer to the SAME shared daily and
    monthly ledger ceilings inside credit_tracker, so this cannot be used to
    spend past the account's limits.
    """
    ceiling = (
        config.SOCIAL_MAX_DISCOVERY_CREDITS_PER_RUN if max_credits is None else max_credits
    )
    return InfluencerDiscovery(
        enabled=enabled and bool(config.INFLUENCERS_API_KEY),
        max_credits=ceiling,
        handle_normalizer=normalize_social_handle,
        # The vendor is the ONLY source of a TikTok/Instagram follower count —
        # there is no free platform API to verify it against — so unlike the
        # YouTube path its statistics must be carried, not dropped.
        carry_vendor_stats=True,
    )


def discover(
    platform: str,
    *,
    lane: dict,
    target: int,
    exclude_handles=(),
    client: InfluencerDiscovery | None = None,
) -> list[dict]:
    """
    Candidates for one platform and one lane. Never raises; [] on any failure.

    Each candidate is {handle, channel_title, influencers_user_id,
    matched_keywords} — IDENTIFIERS ONLY, exactly as the YouTube path receives
    them. The vendor's `followers` and `engagement_percent` are dropped by
    `_to_candidate` for the same reason as there: a purchased statistic must not
    sit in a field the gates and Airtable columns treat as measured. Followers
    are re-read for gating from the posts screen.
    """
    platform = (platform or "").lower()
    if platform not in SUPPORTED:
        raise ValueError(f"unsupported social platform {platform!r}")

    disc = client or client_for_run()
    if not disc.enabled:
        logger.info("social discovery inactive (no API key or budget) for %s", platform)
        return []

    filters = build_filters(platform, lane)
    label = f"social discovery {platform}/{lane.get('key', 'unlabelled')}"

    try:
        return disc.discover(
            filters=filters,
            target=target,
            exclude_handles=exclude_handles,
            platform=platform,
            sort=DEFAULT_SORT,
            source_label=label,
        )
    except Exception as exc:  # fail-soft, matching the YouTube contract
        logger.warning("social discovery failed for %s: %s", label, exc)
        return []
