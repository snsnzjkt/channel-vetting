"""
One-off maintenance script: fills in the Email field for records already
sitting in Airtable that don't have one yet.

Does NOT re-run discovery (no search.list calls, no new candidates) — it
re-enriches channels already tracked (channels.list + playlistItems.list +
videos.list, ~3 quota units per channel), then re-runs main.py's full
email fallback chain: the two free description-based steps, the older-
uploads scan (2 more units per extra page), and an optional Playwright +
stealth pass over the public About page.

--limit caps how many missing-email records are processed per niche, so
you can run this in controlled batches instead of all at once.

Deliberately makes NO direct HTTP calls of its own: every request it
causes goes out through airtable_client / enrichment / browser_email, so
it inherits the shared retrying sessions in http_client.py for free.
That's why there is no `requests` or `http_client` import here — if you
add a call site, route it through the shared session (http_client.AIRTABLE
or .YOUTUBE) rather than reaching for a bare `requests.get()`.
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
from influencers import InfluencersClient
from enrichment import (
    get_channel_stats,
    get_recent_video_performance,
    FREEMAIL_DOMAINS,
)
from main import csv_safe, resolve_email_with_source
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
    enricher=None,
) -> dict:
    """
    Re-run the email fallback chain over one niche's email-less records.

    `scraper` is a browser_email.BrowserEmailScraper (or null_scraper())
    and `enricher` an influencers.InfluencersClient (or null_client()),
    both shared across every channel and every niche in this run — see
    resolve_email() in main.py for how they fit into the fallback chain.
    The single `enricher` is what keeps the run's lookup budget and its
    credit-cap breaker shared rather than resetting per table.

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

        # The chain reports which of its five steps produced the address —
        # inferring it here by comparing the result back against
        # stats/performance cannot tell the older-uploads scan, the
        # influencers.club lookup and the browser pass apart, since none of
        # the three is echoed in either dict.
        email, source = resolve_email_with_source(
            stats, performance, scraper, enricher
        )
        title = (stats.get("channel_title") or "")[:40]
        if email:
            by_source[source] += 1
            if email.rsplit("@", 1)[-1].lower() in FREEMAIL_DOMAINS:
                freemail += 1

            # csv_safe() for the same reason main.py applies it on the normal
            # path: this address can come from browser_email.py scraping an
            # arbitrary third-party site, and a value starting with =/+/-/@
            # becomes a live formula when the reviewer exports the table to
            # CSV and opens it in Excel. Airtable itself is not a formula
            # context, so this looks unnecessary right up until someone
            # exports.
            push_record(table_name, {"Channel ID": channel_id, "Email": csv_safe(email)})
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
        "--use-playwright-stealth",
        "--use-cloakbrowser",
        dest="use_playwright_stealth",
        action="store_true",
        help="After the free text-based steps, try the channel About page in Playwright + stealth.",
    )
    args = parser.parse_args()
    print(f"Playwright+stealth fallback: {'ENABLED' if args.use_playwright_stealth else 'DISABLED'}\n")

    totals = {"checked": 0, "unreachable": 0, "found": 0, "freemail": 0}
    by_source: Counter = Counter()
    scraper = BrowserEmailScraper.launch() if args.use_playwright_stealth else null_scraper()
    # Email chain step 4. This script exists to re-run the chain over rows
    # that have no address, which is exactly the population step 4 was added
    # for — omitting it here would leave the tool unable to find (or report)
    # anything the new step contributes.
    enricher = InfluencersClient.from_config()
    # Said out loud for the same reason the Playwright line above is: this
    # is the only step that costs money, and "the key is set" and "the step
    # is live" are different facts.
    print(f"influencers.club step 4:     {'ENABLED' if enricher.enabled else 'DISABLED'}\n")
    try:
        for niche_name, table_name in TABLES.items():
            result = backfill_table(
                niche_name, table_name, args.limit, scraper, enricher
            )
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
    # Reported here even more than in main.py: get_records_missing_email()
    # selects rows that by definition have no address, so nearly every
    # record reaches step 4 — this is the higher-burn caller of the two.
    if enricher.lookups_spent:
        print(
            f"influencers.club:    {enricher.lookups_spent} billable lookup(s), "
            f"{enricher.credits_reported:g} credits reported by the vendor"
        )
    if by_source:
        print("\nFound by step:")
        for source, count in by_source.most_common():
            print(f"  {count:>4}  {source}")


if __name__ == "__main__":
    main()
