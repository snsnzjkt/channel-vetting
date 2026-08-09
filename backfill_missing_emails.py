"""
One-off maintenance script: fills in the Email field for records already
sitting in Airtable that don't have one yet.

Does NOT re-run discovery (no search.list calls, no new candidates) — it
re-enriches channels already tracked (channels.list + playlistItems.list +
videos.list, ~3 quota units per channel) and runs them through the free
email fallback chain plus an optional CloakBrowser pass over the public
About page.

Usage:
    python backfill_missing_emails.py [--limit N]

--limit caps how many missing-email records are processed per niche, so
you can run this in controlled batches instead of all at once.
"""
import argparse
import logging
import sys
import time
from collections import Counter

# Channel titles can contain characters outside Windows' default console
# codepage (cp1252) — without this, printing one crashes the run partway
# through with UnicodeEncodeError (this bit an earlier full run).
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from airtable_client import get_records_missing_email, push_record
from browser_email import BrowserEmailScraper, null_scraper
from enrichment import (
    get_channel_stats,
    get_recent_video_performance,
    FREEMAIL_DOMAINS,
)
from main import resolve_email
from config import API_SLEEP_SECONDS, AIRTABLE_TABLE_HOME_THEATER, AIRTABLE_TABLE_LIFESTYLE_SOFA

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TABLES = {
    "Home Theater": AIRTABLE_TABLE_HOME_THEATER,
    "Lifestyle Sofa": AIRTABLE_TABLE_LIFESTYLE_SOFA,
}


def backfill_table(
    niche_name: str,
    table_name: str,
    limit: int | None,
    scraper,
) -> dict:
    """
    Re-run the email fallback chain over one niche's email-less records.

    `scraper` is a browser_email.BrowserEmailScraper (or null_scraper())
    shared across every channel and every niche in this run — see
    resolve_email() in main.py for how it fits into the fallback chain.

    Returns a stats dict: how many records were considered, how many were
    unreachable (private/deleted/no videos), and how many emails were
    found — broken down by which step of the chain produced them, so a
    run tells you *why* coverage moved, not just that it did.
    """
    if not table_name:
        logger.error("No table configured for niche '%s' — skipping.", niche_name)
        return {"checked": 0, "unreachable": 0, "found": 0, "by_source": Counter(), "freemail": 0}

    channel_ids = get_records_missing_email(table_name)
    if limit:
        channel_ids = channel_ids[:limit]
    total = len(channel_ids)
    logger.info("'%s': processing %d record(s) missing an email.", niche_name, total)

    found = 0
    unreachable = 0
    by_source: Counter = Counter()
    freemail = 0

    for i, channel_id in enumerate(channel_ids, start=1):
        stats = get_channel_stats(channel_id)
        time.sleep(API_SLEEP_SECONDS)
        if stats is None:
            unreachable += 1
            continue

        performance = get_recent_video_performance(channel_id, stats.get("uploads_playlist_id"))
        time.sleep(API_SLEEP_SECONDS)
        if performance is None:
            unreachable += 1
            continue

        email = resolve_email(stats, performance, scraper)
        title = (stats.get("channel_title") or "")[:40]
        if email:
            # Attribute the hit to the step that actually produced it.
            if email == performance.get("repeated_email"):
                source = f"repeated across videos (scanned {performance.get('email_scan_size', '?')})"
            elif email == stats.get("business_email"):
                source = "About description"
            else:
                # With Hunter/Modash gone, resolve_email()'s only other
                # source is the CloakBrowser scraper — if the email
                # didn't come from repeated_email or business_email, it
                # came from here. (The old `elif scraper.enabled: ...
                # else: "About description"` duplicated this same label
                # under an unreachable branch: scraper.enabled is False
                # exactly when null_scraper() is in play, which always
                # returns "" and so could never produce a matching email
                # to reach this branch in the first place.)
                source = "CloakBrowser visible text"
            by_source[source] += 1
            if email.rsplit("@", 1)[-1].lower() in FREEMAIL_DOMAINS:
                freemail += 1

            push_record(table_name, {"Channel ID": channel_id, "Email": email})
            found += 1
            print(f"[{niche_name}] {i}/{total} [FOUND] {title} -> {email}  ({source})")
        else:
            print(f"[{niche_name}] {i}/{total} [no email] {title}")

        time.sleep(API_SLEEP_SECONDS)

    return {
        "checked": total,
        "unreachable": unreachable,
        "found": found,
        "by_source": by_source,
        "freemail": freemail,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill missing Email values for already-tracked channels")
    parser.add_argument("--limit", type=int, default=None, help="Max records to process per niche")
    parser.add_argument(
        "--use-cloakbrowser",
        action="store_true",
        help="After the free text-based steps, try the channel About page in CloakBrowser.",
    )
    args = parser.parse_args()
    print(f"CloakBrowser fallback: {'ENABLED' if args.use_cloakbrowser else 'DISABLED'}\n")

    totals = {"checked": 0, "unreachable": 0, "found": 0, "freemail": 0}
    by_source: Counter = Counter()
    scraper = BrowserEmailScraper.launch() if args.use_cloakbrowser else null_scraper()
    try:
        for niche_name, table_name in TABLES.items():
            result = backfill_table(niche_name, table_name, args.limit, scraper)
            for key in totals:
                totals[key] += result[key]
            by_source.update(result["by_source"])
    finally:
        scraper.close()

    checked = totals["checked"]
    reachable = checked - totals["unreachable"]
    print("\n--- Backfill summary ---")
    print(f"Records checked:     {checked}")
    print(f"Unreachable:         {totals['unreachable']}  (private/deleted/no videos)")
    print(f"Reachable:           {reachable}")
    print(f"Emails found:        {totals['found']}"
          + (f"  ({totals['found'] / reachable * 100:.1f}% of reachable)" if reachable else ""))
    print(f"  of those, freemail: {totals['freemail']}  (would have been discarded before the blocklist split)")
    print(f"Still missing:       {checked - totals['found']}")
    if by_source:
        print("\nFound by step:")
        for source, count in by_source.most_common():
            print(f"  {count:>4}  {source}")


if __name__ == "__main__":
    main()
