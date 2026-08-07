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
import time
from datetime import date

import pandas as pd

from discovery import run_discovery
from enrichment import get_channel_stats, get_recent_video_performance, calc_upload_frequency
from scoring import calc_fake_follower_risk, calc_overall_score
from airtable_client import get_existing_channel_ids, push_record
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


def process_candidate(candidate: dict) -> dict | None:
    """Enrich, score, and build an Airtable record for one candidate channel."""
    channel_id = candidate["channel_id"]

    stats = get_channel_stats(channel_id)
    time.sleep(API_SLEEP_SECONDS)
    if stats is None:
        return None  # private/deleted/inaccessible — already logged by enrichment.py

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
        # Prefer an email repeated across multiple recent videos (a much
        # stronger "this is their real contact" signal) over a one-off
        # mention in the channel's About description.
        "Email": performance.get("repeated_email") or stats.get("business_email", ""),
        "Fake Follower Risk Score": fake_risk,
        "Overall Score": overall_score,
        "Status": DEFAULT_STATUS,
        "Source": f"{SOURCE_LABEL} ({', '.join(candidate.get('matched_keywords', []))})",
        "Notes": "",
        "Date Added": date.today().isoformat(),
    }
    return record


def run_niche(niche_name: str, table_name: str, keywords: list[str], max_results_per_keyword: int, days_back: int) -> tuple[int, int]:
    """Run discovery -> pre-filter -> enrich -> score -> push for one niche's table. Returns (discovered, processed)."""
    if not table_name:
        logger.error(
            "No Airtable table configured for niche '%s' — set the matching env var. Skipping this niche.",
            niche_name,
        )
        return 0, 0

    logger.info("=== Niche: %s (table: %s) ===", niche_name, table_name)
    logger.info("Starting discovery for %d keyword(s)...", len(keywords))
    discovered = run_discovery(keywords, max_results_per_keyword=max_results_per_keyword, days_back=days_back)
    logger.info("Discovered %d unique candidate channel(s).", len(discovered))

    existing_ids = get_existing_channel_ids(table_name)

    # A DataFrame makes the pre-filter step easy to extend later (e.g.
    # sorting/inspecting candidates by matched keyword count before
    # spending enrichment quota on them).
    candidates_df = pd.DataFrame(discovered)
    if candidates_df.empty:
        logger.info("No candidates discovered — nothing to process.")
        new_candidates = []
    else:
        candidates_df["already_tracked"] = candidates_df["channel_id"].isin(existing_ids)
        new_candidates = candidates_df[~candidates_df["already_tracked"]].to_dict("records")

    logger.info(
        "%d candidate(s) already tracked in '%s', %d remaining to process.",
        len(discovered) - len(new_candidates), table_name, len(new_candidates),
    )

    processed = 0
    for candidate in new_candidates:
        record = process_candidate(candidate)
        if record is None:
            continue

        pushed = push_record(table_name, record)
        processed += 1
        status_note = "OK" if pushed else "AIRTABLE PUSH FAILED"
        print(
            f"[{niche_name}] [{status_note}] {record['Channel Name']} "
            f"| score={record['Overall Score']} "
            f"| fake_risk={record['Fake Follower Risk Score']}"
        )
        time.sleep(API_SLEEP_SECONDS)

    return len(discovered), processed


def run(niches: dict, max_results_per_keyword: int, days_back: int) -> None:
    total_discovered = 0
    total_processed = 0

    for niche_name, niche_config in niches.items():
        discovered, processed = run_niche(
            niche_name,
            niche_config["table_name"],
            niche_config["keywords"],
            max_results_per_keyword,
            days_back,
        )
        total_discovered += discovered
        total_processed += processed

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
