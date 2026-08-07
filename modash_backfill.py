"""
Fills the Email field for Airtable records the free extraction path
couldn't resolve, using Modash's creator database.

Costs no YouTube quota at all — Modash is keyed on the channel ID that's
already sitting in Airtable, so this runs without touching the YouTube
API. The only meter is Modash credits: 1 per channel looked up.

Run this AFTER backfill_missing_emails.py --no-hunter has exhausted the
free path, so credits are only ever spent on channels whose email genuinely
isn't in their About text or recent video descriptions.

Usage:
    python modash_backfill.py --limit 25      # spend at most 25 credits
    python modash_backfill.py --dry-run       # preflight only, spends nothing
    python modash_backfill.py                 # process every missing record

--limit caps credits spent per niche. The script always checks the account
balance first and refuses to start a batch it cannot pay for.
"""
import argparse
import logging
import sys
import time
from collections import Counter

# Channel titles can contain characters outside Windows' default console
# codepage (cp1252) — without this, printing one crashes the run partway
# through with UnicodeEncodeError.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from airtable_client import get_records_missing_email, push_record
from enrichment import EMAIL_DOMAIN_BLOCKLIST, FREEMAIL_DOMAINS
from modash_client import (
    get_account_info,
    find_channel_email,
    FOUND,
    NO_EMAIL_ON_FILE,
    NOT_IN_DATABASE,
    ERROR,
)
from config import AIRTABLE_TABLE_HOME_THEATER, AIRTABLE_TABLE_LIFESTYLE_SOFA

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TABLES = {
    "Home Theater": AIRTABLE_TABLE_HOME_THEATER,
    "Lifestyle Sofa": AIRTABLE_TABLE_LIFESTYLE_SOFA,
}

# Modash's own rate limit is reported per account; this is just a polite
# floor between calls so a large batch doesn't burst into a 429.
SLEEP_BETWEEN_LOOKUPS = 0.4

# If this many consecutive lookups fail with NOT_IN_DATABASE, the
# identifier format is probably wrong rather than the creators being
# genuinely absent — bail out instead of burning the rest of the balance
# proving the same point 400 more times.
CONSECUTIVE_MISS_ABORT = 15


def preflight() -> int | None:
    """
    Check the Modash account can actually serve requests before any
    credit is spent. Returns the available credit balance, or None if the
    account isn't usable (caller should abort).
    """
    info = get_account_info()
    if info is None:
        print("Could not reach Modash. Check MODASH_API_KEY in .env.")
        return None

    credits = info.get("credits") or 0
    rate_limit = info.get("rate_limit") or 0
    print(f"Modash account: {credits} credit(s), rate limit {rate_limit}/min")

    # A valid key on an unprovisioned account authenticates fine but is
    # authorized for nothing: credits and rate limit both read 0 and every
    # report call comes back 429 "Please contact support." Catch that here
    # rather than letting the caller discover it one failed lookup at a time.
    if rate_limit == 0:
        print(
            "\nThis account has a rate limit of 0 — the API key is valid but not\n"
            "authorized to make Discovery API calls. Modash's free trial covers the\n"
            "web app only; API access is provisioned separately. Contact Modash to\n"
            "enable it, then re-run."
        )
        return None
    if credits <= 0:
        print("\nNo credits remaining — top up or wait for the next billing period.")
        return None

    return credits


def process_table(niche_name: str, table_name: str, limit: int | None, budget: int) -> dict:
    """
    Look up every email-less record in one niche's table via Modash.

    `budget` caps how many credits this table may spend. Returns a stats
    dict including a breakdown of *why* each miss missed, since "Modash
    has no email for them" and "Modash has never heard of them" have very
    different implications for whether the tool is worth buying.
    """
    empty = {"checked": 0, "found": 0, "spent": 0, "freemail": 0, "outcomes": Counter(), "aborted": False}
    if not table_name:
        logger.error("No table configured for niche '%s' — skipping.", niche_name)
        return empty

    channel_ids = get_records_missing_email(table_name)
    if limit:
        channel_ids = channel_ids[:limit]
    channel_ids = channel_ids[:budget]
    total = len(channel_ids)
    if not total:
        print(f"[{niche_name}] nothing to do.")
        return empty

    print(f"\n[{niche_name}] looking up {total} channel(s) — up to {total} credit(s).")

    found = spent = freemail = 0
    outcomes: Counter = Counter()
    consecutive_misses = 0
    aborted = False

    for i, channel_id in enumerate(channel_ids, start=1):
        status, email = find_channel_email(channel_id)
        outcomes[status] += 1
        # Only successful reports are billed; 5xx/429 failures are not.
        if status in (FOUND, NO_EMAIL_ON_FILE):
            spent += 1

        if status == NOT_IN_DATABASE:
            consecutive_misses += 1
            if consecutive_misses >= CONSECUTIVE_MISS_ABORT:
                print(
                    f"\n[{niche_name}] ABORTED — {consecutive_misses} consecutive "
                    f"'not in database' results. The channel ID format is likely not "
                    f"what Modash expects; stopping before more credits are spent."
                )
                aborted = True
                break
        else:
            consecutive_misses = 0

        if status == FOUND:
            domain = email.rsplit("@", 1)[-1].lower()
            # Same third-party screen the free path uses (sponsors, tip
            # jars, platforms). Freemail is deliberately NOT screened —
            # a creator's gmail is exactly what we're paying to find.
            if domain in EMAIL_DOMAIN_BLOCKLIST:
                outcomes["rejected_third_party"] += 1
                outcomes[FOUND] -= 1
                print(f"[{niche_name}] {i}/{total} [rejected] {channel_id} -> {email} (third-party domain)")
            else:
                if domain in FREEMAIL_DOMAINS:
                    freemail += 1
                push_record(table_name, {"Channel ID": channel_id, "Email": email})
                found += 1
                print(f"[{niche_name}] {i}/{total} [FOUND] {channel_id} -> {email}")
        else:
            label = {
                NO_EMAIL_ON_FILE: "no email on file",
                NOT_IN_DATABASE: "not in Modash",
                ERROR: "error",
            }.get(status, status)
            print(f"[{niche_name}] {i}/{total} [{label}] {channel_id}")

        time.sleep(SLEEP_BETWEEN_LOOKUPS)

    return {
        "checked": i if total else 0,
        "found": found,
        "spent": spent,
        "freemail": freemail,
        "outcomes": outcomes,
        "aborted": aborted,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill missing Emails via the Modash creator database")
    parser.add_argument("--limit", type=int, default=None, help="Max records (credits) to spend per niche")
    parser.add_argument("--dry-run", action="store_true", help="Run the preflight check only; spend nothing")
    args = parser.parse_args()

    credits = preflight()
    if credits is None:
        sys.exit(1)
    if args.dry_run:
        print("\n--dry-run: preflight passed, no lookups performed.")
        return

    totals = {"checked": 0, "found": 0, "spent": 0, "freemail": 0}
    outcomes: Counter = Counter()
    remaining_budget = credits

    for niche_name, table_name in TABLES.items():
        if remaining_budget <= 0:
            print(f"\n[{niche_name}] skipped — credit budget exhausted.")
            continue
        result = process_table(niche_name, table_name, args.limit, remaining_budget)
        for key in totals:
            totals[key] += result[key]
        outcomes.update(result["outcomes"])
        remaining_budget -= result["spent"]
        if result["aborted"]:
            break

    checked = totals["checked"]
    print("\n--- Modash backfill summary ---")
    print(f"Channels looked up:  {checked}")
    print(f"Credits spent:       {totals['spent']}")
    print(f"Emails found:        {totals['found']}"
          + (f"  ({totals['found'] / checked * 100:.1f}% fill rate)" if checked else ""))
    print(f"  of those, freemail: {totals['freemail']}  (unreachable by Hunter-style tools)")
    if outcomes:
        print("\nOutcome breakdown:")
        for status, count in outcomes.most_common():
            print(f"  {count:>4}  {status}")
    print(f"\nCredits remaining:   ~{max(0, credits - totals['spent'])}")


if __name__ == "__main__":
    main()
