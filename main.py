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
from datetime import date

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
    extract_candidate_domain,
)
from hunter_client import find_domain_email
from scoring import calc_fake_follower_risk, calc_overall_score
from airtable_client import get_existing_channel_ids, push_record
from external_dedupe import fetch_external_handles
from quota_tracker import get_today_spend
from config import (
    API_SLEEP_SECONDS,
    DEFAULT_STATUS,
    SOURCE_LABEL,
    DAILY_QUOTA_BUDGET,
    AIRTABLE_TABLE_HOME_THEATER,
    AIRTABLE_TABLE_LIFESTYLE_SOFA,
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
    },
}

# Niche match currently defaults to a neutral midpoint (50/100) since
# automated topical matching isn't implemented yet — human reviewers can
# override the "Overall Score" judgment during Airtable review. Wire in a
# real niche classifier here if/when one becomes available.
DEFAULT_NICHE_MATCH = 50.0


def resolve_email(stats: dict, performance: dict, use_hunter: bool = True) -> str:
    """
    Email fallback chain, shared by both new-candidate processing and the
    backfill_missing_emails.py maintenance script: repeated-video email
    (strongest) -> About description mention -> Hunter.io Domain Search
    (last resort, only spends a Hunter credit when the two free methods
    found nothing and HUNTER_API_KEY is configured).

    Pass use_hunter=False to stop after the two free steps — useful when
    measuring what the free extraction alone yields, or when deliberately
    conserving Hunter credits for a later pass.
    """
    email = performance.get("repeated_email") or stats.get("business_email", "")
    if not email and use_hunter:
        # Deliberately the channel's own About description ONLY, not video
        # descriptions. Video descriptions are dominated by per-video
        # sponsor/affiliate links (e.g. "Check out Coupert with my link
        # here..."), and feeding one of those to Hunter returns that
        # SPONSOR's business email, not the creator's — a real false
        # positive observed in testing (a sponsor link produced a
        # confidently-wrong "found" email). The About description is
        # curated once by the creator and far less likely to rotate
        # through different sponsors, though not a complete guarantee.
        domain = extract_candidate_domain(stats.get("description", ""))
        if domain:
            email = find_domain_email(domain)
            time.sleep(API_SLEEP_SECONDS)
    return email


def process_candidate(candidate: dict, external_handles: dict[str, str]) -> dict | None:
    """Enrich, score, and build an Airtable record for one candidate channel."""
    channel_id = candidate["channel_id"]

    stats = get_channel_stats(channel_id)
    time.sleep(API_SLEEP_SECONDS)
    if stats is None:
        return None  # private/deleted/inaccessible — already logged by enrichment.py

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
        return None

    performance = get_recent_video_performance(channel_id, stats.get("uploads_playlist_id"))
    time.sleep(API_SLEEP_SECONDS)
    if performance is None:
        logger.info("Skipping %s — no accessible recent video performance data.", stats.get("channel_title"))
        return None

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

    email = resolve_email(stats, performance)

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
        "Status": DEFAULT_STATUS,
        "Source": f"{SOURCE_LABEL} ({', '.join(candidate.get('matched_keywords', []))})",
        "Notes": "",
        "Date Added": date.today().isoformat(),
    }
    return record


def run_niche(
    niche_name: str,
    table_name: str,
    keywords: list[str],
    max_results_per_keyword: int,
    days_back: int,
    globally_tracked_ids: set[str],
    external_handles: dict[str, str],
) -> tuple[int, int, set[str]]:
    """
    Run discovery -> pre-filter -> enrich -> score -> push for one niche's
    table. `globally_tracked_ids` is the base-wide dedupe set (union of
    every niche table's Channel IDs, not just this one) — a candidate
    already tracked in ANY niche's table is skipped here too, so a
    channel is claimed by whichever niche discovers it first rather than
    being trackable in more than one table.

    Returns (discovered_count, processed_count, newly_tracked_ids) — the
    caller merges newly_tracked_ids into the shared dedupe set so a later
    niche in the same run also sees channels just pushed by this one.
    """
    if not table_name:
        logger.error(
            "No Airtable table configured for niche '%s' — set the matching env var. Skipping this niche.",
            niche_name,
        )
        return 0, 0, set()

    logger.info("=== Niche: %s (table: %s) ===", niche_name, table_name)
    logger.info("Starting discovery for %d keyword(s)...", len(keywords))
    discovered = run_discovery(keywords, max_results_per_keyword=max_results_per_keyword, days_back=days_back)
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

    processed = 0
    newly_tracked_ids: set[str] = set()
    for candidate in new_candidates:
        record = process_candidate(candidate, external_handles)
        if record is None:
            continue

        pushed = push_record(table_name, record)
        processed += 1
        if pushed:
            newly_tracked_ids.add(candidate["channel_id"])
        status_note = "OK" if pushed else "AIRTABLE PUSH FAILED"
        print(
            f"[{niche_name}] [{status_note}] {record['Channel Name']} "
            f"| score={record['Overall Score']} "
            f"| fake_risk={record['Fake Follower Risk Score']}"
        )
        time.sleep(API_SLEEP_SECONDS)

    return len(discovered), processed, newly_tracked_ids


def run(niches: dict, max_results_per_keyword: int, days_back: int) -> None:
    # Global (base-wide) dedupe: fetched once across every niche's table
    # before any niche runs, so a channel already tracked anywhere in the
    # base — not just in the niche currently being processed — is skipped.
    globally_tracked_ids: set[str] = set()
    for niche_config in niches.values():
        if niche_config["table_name"]:
            globally_tracked_ids |= get_existing_channel_ids(niche_config["table_name"])

    # Handles already tracked in the base's other YouTube outreach/leads/
    # influencer tables (see external_dedupe.py) — cached, so this is
    # near-instant on any run within EXTERNAL_CACHE_MAX_AGE_HOURS of the
    # last one.
    external_handles = fetch_external_handles()

    total_discovered = 0
    total_processed = 0

    for niche_name, niche_config in niches.items():
        discovered, processed, newly_tracked_ids = run_niche(
            niche_name,
            niche_config["table_name"],
            niche_config["keywords"],
            max_results_per_keyword,
            days_back,
            globally_tracked_ids,
            external_handles,
        )
        total_discovered += discovered
        total_processed += processed
        # So a later niche in this same run also sees channels this one
        # just pushed, rather than only picking up prior runs' state.
        globally_tracked_ids |= newly_tracked_ids

    quota_used = get_today_spend()
    print("\n--- Run summary ---")
    print(f"Total discovered:  {total_discovered}")
    print(f"Total processed:   {total_processed}")
    print(f"Quota used today:  {quota_used} / {DAILY_QUOTA_BUDGET}")


def main() -> None:
    parser = argparse.ArgumentParser(description="YouTube channel vetting pipeline")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run a cheap end-to-end smoke test: 1 keyword, max_results=5, first niche only.",
    )
    args = parser.parse_args()

    if args.test:
        logger.info("Running in --test mode: 1 keyword, max_results=5, first niche only.")
        first_niche_name = next(iter(NICHES))
        first_niche = NICHES[first_niche_name]
        test_niches = {first_niche_name: {**first_niche, "keywords": first_niche["keywords"][:1]}}
        run(niches=test_niches, max_results_per_keyword=5, days_back=90)
    else:
        run(niches=NICHES, max_results_per_keyword=50, days_back=90)


if __name__ == "__main__":
    main()
