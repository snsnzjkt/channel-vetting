"""
One-off maintenance script: finds Channel Prospects records that are
already tracked in one of the other YouTube-tracking tables elsewhere in
the base (see external_dedupe.py), and removes them from OUR two
Prospects tables only — the other tables are never modified.

Dry-run by default: lists every match it would delete, without deleting
anything. Pass --confirm to actually delete after reviewing the list.

Usage:
    python cleanup_external_duplicates.py              # dry run
    python cleanup_external_duplicates.py --confirm     # actually delete
"""
import argparse
import logging
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

from airtable_client import _base_url, _headers, delete_record
from enrichment import get_channel_stats
from external_dedupe import fetch_external_handles
from config import (
    API_SLEEP_SECONDS,
    AIRTABLE_TABLE_HOME_THEATER,
    AIRTABLE_TABLE_LIFESTYLE_SOFA,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TABLES = {
    "Home Theater": AIRTABLE_TABLE_HOME_THEATER,
    "Lifestyle Sofa": AIRTABLE_TABLE_LIFESTYLE_SOFA,
}


def _fetch_all_prospects(table_name: str) -> list[dict]:
    """Every record's Airtable record ID, Channel ID, and Channel Name."""
    prospects = []
    offset = None
    while True:
        params = {"fields[]": ["Channel ID", "Channel Name"], "pageSize": 100}
        if offset:
            params["offset"] = offset
        resp = requests.get(_base_url(table_name), headers=_headers(), params=params, timeout=30)
        data = resp.json()
        for record in data.get("records", []):
            fields = record.get("fields", {})
            if fields.get("Channel ID"):
                prospects.append({
                    "record_id": record["id"],
                    "channel_id": fields["Channel ID"],
                    "channel_name": fields.get("Channel Name", ""),
                })
        offset = data.get("offset")
        if not offset:
            break
        time.sleep(API_SLEEP_SECONDS)
    return prospects


def find_matches(table_name: str, external_handles: dict[str, str]) -> list[dict]:
    prospects = _fetch_all_prospects(table_name)
    logger.info("Checking %d prospect(s) against the external handle index...", len(prospects))

    matches = []
    for p in prospects:
        stats = get_channel_stats(p["channel_id"])
        time.sleep(API_SLEEP_SECONDS)
        if stats is None:
            continue
        handle = stats.get("handle", "")
        if handle and handle in external_handles:
            matches.append({
                **p,
                "handle": handle,
                "matched_in": external_handles[handle],
            })
    return matches


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove Prospects already tracked in other base tables")
    parser.add_argument("--confirm", action="store_true", help="Actually delete matches instead of dry-run listing")
    parser.add_argument("--refresh-external-cache", action="store_true", help="Force-refresh the external handle index instead of using the cache")
    args = parser.parse_args()

    external_handles = fetch_external_handles(force_refresh=args.refresh_external_cache)
    logger.info("External handle index ready: %d unique handle(s).", len(external_handles))

    all_matches = {}
    for niche_name, table_name in TABLES.items():
        if not table_name:
            continue
        matches = find_matches(table_name, external_handles)
        all_matches[niche_name] = (table_name, matches)

    print("\n--- Matches found ---")
    total = 0
    for niche_name, (table_name, matches) in all_matches.items():
        for m in matches:
            print(f"[{niche_name}] {m['channel_name']} (@{m['handle']}) -> already in '{m['matched_in']}'")
            total += 1
    print(f"\nTotal matches: {total}")

    if not args.confirm:
        print("\nDry run only — no records deleted. Re-run with --confirm to delete these.")
        return

    deleted = 0
    for niche_name, (table_name, matches) in all_matches.items():
        for m in matches:
            ok = delete_record(table_name, m["record_id"])
            if ok:
                deleted += 1
                print(f"[{niche_name}] [DELETED] {m['channel_name']}")
            else:
                print(f"[{niche_name}] [DELETE FAILED] {m['channel_name']}")
            time.sleep(API_SLEEP_SECONDS)

    print(f"\nDeleted {deleted}/{total} matched record(s).")


if __name__ == "__main__":
    main()
