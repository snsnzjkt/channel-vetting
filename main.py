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

import pandas as pd

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
)
from scoring import calc_fake_follower_risk, calc_overall_score, QUALIFIED, qualify
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
    USE_CLOAKBROWSER,
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
        # From the Lifestyle Sofa brief: "Has min of 2k+ view on YouTube
        # videos". The brief sets no channel-age requirement. Its
        # Instagram thresholds (100k+ followers, 20k+ reel views) are out
        # of scope — this pipeline only observes YouTube.
        "min_avg_views": 2_000,
        "min_channel_age_months": None,
    },
}

# Niche match currently defaults to a neutral midpoint (50/100) since
# automated topical matching isn't implemented yet — human reviewers can
# override the "Overall Score" judgment during Airtable review. Wire in a
# real niche classifier here if/when one becomes available.
DEFAULT_NICHE_MATCH = 50.0


def resolve_email(stats: dict, performance: dict, scraper=None) -> str:
    """
    Email fallback chain, cheapest and strongest signal first:

      1. An address repeated across several recent video descriptions.
      2. A single mention in the channel's own About description.
      3. The rendered About page, read in CloakBrowser.

    Steps 1-2 use data already fetched during enrichment and cost
    nothing. Step 3 only runs when both found nothing, and only when a
    scraper is supplied.
    """
    email = performance.get("repeated_email") or stats.get("business_email", "")
    if not email and scraper is not None:
        email = scraper.find_email(stats["channel_id"])
    return email


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

    qualification = qualify(
        performance["avg_views"],
        channel_age_months(stats.get("published_at", "")),
        niche_config["min_avg_views"],
        niche_config["min_channel_age_months"],
    )

    email = resolve_email(stats, performance, scraper)

    # Checkpoint 3 — catches agency addresses shared across channels.
    if email:
        hit = blocklist.match(email=email)
        if hit:
            logger.info("BLOCKED %s — DO NOT CONTACT (%s).", stats.get("channel_title"), hit)
            return None, "blocked"

    record = {
        "Channel Name": stats["channel_title"],
        "Channel URL": f"https://www.youtube.com/channel/{channel_id}",
        "Channel ID": channel_id,
        "Subscriber Count": stats["subscriber_count"],
        "Avg Views (last 10 videos)": round(performance["avg_views"], 1),
        "Engagement Rate": round(performance["avg_engagement_rate"], 2),
        # "Upload Frequency" is a text field in Airtable (not Number) — it
        # rejects raw JSON numbers, so this must be sent as a string.
        # Rounded to a whole number for display; the unrounded value is
        # still what feeds calc_overall_score above.
        "Upload Frequency": f"{round(upload_freq)} videos/month",
        # Best-effort: most creators never set defaultAudioLanguage/
        # defaultLanguage on their videos, so this is frequently "Unknown".
        # Channel *country* (stats["country"]) is a separate signal and is
        # deliberately not used here, since it isn't the same thing as the
        # content's spoken language.
        "Content Language": performance.get("content_language") or "Unknown",
        "Email": email,
        "Fake Follower Risk Score": fake_risk,
        "Overall Score": overall_score,
        "Qualification": qualification,
        "Status": DEFAULT_STATUS,
        "Source": f"{SOURCE_LABEL} ({', '.join(candidate.get('matched_keywords', []))})",
        "Notes": "",
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

    target_fresh = int((qualified_headroom + flagged_headroom) * CANDIDATE_OVERSHOOT)
    logger.info("Starting discovery for %d keyword(s)...", len(keywords))
    discovered = run_discovery(
        keywords,
        max_results_per_keyword=max_results_per_keyword,
        days_back=days_back,
        exclude_ids=globally_tracked_ids,
        target_fresh=target_fresh,
    )
    logger.info("Discovered %d unique candidate channel(s).", len(discovered))

    # A DataFrame makes the pre-filter step easy to extend later (e.g.
    # sorting/inspecting candidates by matched keyword count before
    # spending enrichment quota on them).
    candidates_df = pd.DataFrame(discovered)
    if candidates_df.empty:
        logger.info("No candidates discovered — nothing to process.")
        new_candidates = []
    else:
        candidates_df["already_tracked"] = candidates_df["channel_id"].isin(globally_tracked_ids)
        new_candidates = candidates_df[~candidates_df["already_tracked"]].to_dict("records")

    logger.info(
        "%d candidate(s) already tracked elsewhere in the base, %d remaining to process.",
        len(discovered) - len(new_candidates), len(new_candidates),
    )

    counts = push_until_full(
        new_candidates,
        lambda c: process_candidate(c, external_handles, blocklist, niche_config, scraper),
        table_name,
        qualified_headroom,
        flagged_headroom,
    )

    logger.info(
        "'%s': pushed %d qualified, %d flagged, skipped %d.",
        niche_name, counts["qualified"], counts["flagged"], counts["skipped"],
    )

    if counts["qualified"] < qualified_headroom:
        logger.warning(
            "'%s' finished under its qualified budget (%d of %d). Discovery is running "
            "dry for these keywords — widen --days-back for a one-off sweep, or add "
            "keywords from the brief's secondary content types.",
            niche_name, counts["qualified"], qualified_headroom,
        )

    return len(discovered), counts["qualified"] + counts["flagged"], counts["pushed_ids"], True


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

    scraper = BrowserEmailScraper.launch() if USE_CLOAKBROWSER else null_scraper()
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
