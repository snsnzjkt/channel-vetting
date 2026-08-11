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
    python cleanup_external_duplicates.py --confirm --yes-delete-many
                                                       # ... even if the
                                                       # match set is huge

SAFETY MODEL — this is the only script in the repo that PERMANENTLY
deletes Airtable rows, and there is no undo on our end
(airtable_client.delete_record). Three independent guards, in the order
they fire:

1. `--confirm` is required. Without it `main()` returns before the delete
   loop is ever reached, having only printed the match list. This is the
   same shape of gate as audit_blocklist.py's `--mark`.
2. A read failure ABORTS instead of returning a partial prospect list.
   Deleting from a table you could only partially read means the printed
   list the human approved isn't the list the script acted on.
3. A mass-delete circuit breaker (`--yes-delete-many`). Mirrors
   audit_blocklist.py's `--yes-create-status-option`: when the script
   cannot vouch that the blast radius is small, it refuses and makes the
   human say so explicitly. The failure this catches is the external
   handle index matching far more than expected — e.g. a
   normalize_handle() regression collapsing distinct handles together —
   which would otherwise wipe out most of both prospect tables in one
   unattended run.

On the retry adapter: DELETE is in http_client.IDEMPOTENT_METHODS, so a
DELETE that hits a 429/5xx is retried. That cannot widen the blast radius
because every DELETE is addressed at one already-known Airtable record ID
— repeating it converges on the same single row gone. The one direction it
CAN mislead is under-reporting: if Airtable deletes the row and the
response is then lost, the retry gets a 404, delete_record() returns
False, and the row is printed as [DELETE FAILED] despite being gone. That
is the safe direction to be wrong in for a destructive script, so it is
accepted rather than papered over — do not "fix" it by treating 404 as
success, which would mask a genuinely wrong record ID.
"""
import argparse
import logging
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# `requests` stays imported for its exception types; the request itself
# goes through the shared retrying session.
import requests

from airtable_client import _base_url, _headers, delete_record
from http_client import AIRTABLE as HTTP, safe_body
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

# Mass-delete circuit breaker (guard 3 in the module docstring). Deleting
# more than DELETE_CAP_WITHOUT_OPT_IN rows, or more than
# DELETE_FRACTION_WITHOUT_OPT_IN of a table's rows, needs
# --yes-delete-many. Both bounds exist because either alone has a blind
# spot: the fraction misses a small table (2 of 3 rows is 67% but is
# hardly a catastrophe), and the absolute cap misses a large one (30 of
# 5,000 rows is fine, 30 of 34 is a wipe-out). The live tables held ~34
# rows total as of 2026-08, but CLAUDE.md warns against treating table
# sizes as fixed, which is exactly why the fraction is here too.
DELETE_CAP_WITHOUT_OPT_IN = 25
DELETE_FRACTION_WITHOUT_OPT_IN = 0.5


class ProspectFetchError(RuntimeError):
    """
    Raised when a prospects table cannot be read completely.

    Deliberately fatal (see guard 2 in the module docstring): the printed
    match list is the artifact a human approves before --confirm, so
    acting on a silently truncated read would delete from a table nobody
    actually reviewed. Reading nothing and deleting nothing is always the
    recoverable outcome here.
    """


def _fetch_all_prospects(table_name: str) -> list[dict]:
    """Every record's Airtable record ID, Channel ID, and Channel Name."""
    prospects = []
    offset = None
    while True:
        params = {"fields[]": ["Channel ID", "Channel Name"], "pageSize": 100}
        if offset:
            params["offset"] = offset
        try:
            resp = HTTP.get(_base_url(table_name), headers=_headers(), params=params, timeout=30)
        except requests.RequestException as e:
            raise ProspectFetchError(f"could not read '{table_name}': {e}") from e

        # This check did not exist before the shared session landed, and
        # its absence was quietly load-bearing in the wrong direction: an
        # error body has no "records" key, so pagination just ended and
        # the caller got however many pages happened to succeed. With the
        # retry adapter, a status that reaches here has already survived
        # ~45s of retries, so treating it as fatal costs nothing that was
        # going to succeed anyway.
        if resp.status_code != 200:
            raise ProspectFetchError(
                f"could not read '{table_name}': {resp.status_code} {safe_body(resp)}"
            )

        try:
            data = resp.json()
        except requests.RequestException as e:
            # requests.exceptions.JSONDecodeError is a RequestException
            # subclass — a 200 carrying a proxy/captive-portal HTML page
            # must not escape as a bare decode error from a script that is
            # about to delete rows.
            raise ProspectFetchError(
                f"could not read '{table_name}': response body was not valid JSON: {e}"
            ) from e

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


def find_matches(
    table_name: str, external_handles: dict[str, str]
) -> tuple[list[dict], int]:
    """
    Returns (matches, prospects_scanned).

    The scanned count is returned rather than just the matches because the
    mass-delete circuit breaker in main() needs a denominator: "12 rows"
    is meaningless without knowing whether the table holds 12 or 1,200.
    """
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
    return matches, len(prospects)


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove Prospects already tracked in other base tables")
    parser.add_argument("--confirm", action="store_true", help="Actually delete matches instead of dry-run listing")
    parser.add_argument("--refresh-external-cache", action="store_true", help="Force-refresh the external handle index instead of using the cache")
    parser.add_argument(
        "--yes-delete-many",
        action="store_true",
        help=(
            f"Proceed with --confirm even when the match set is large "
            f"(> {DELETE_CAP_WITHOUT_OPT_IN} rows, or > "
            f"{DELETE_FRACTION_WITHOUT_OPT_IN:.0%} of a table). A match set that big usually "
            "means the external handle index is matching more than it should, not that the "
            "table really is mostly duplicates. Deletions are permanent — only pass this "
            "after reading the printed list."
        ),
    )
    args = parser.parse_args()

    external_handles = fetch_external_handles(force_refresh=args.refresh_external_cache)
    logger.info("External handle index ready: %d unique handle(s).", len(external_handles))

    all_matches = {}
    for niche_name, table_name in TABLES.items():
        if not table_name:
            continue
        try:
            matches, scanned = find_matches(table_name, external_handles)
        except ProspectFetchError as e:
            # Abort the WHOLE script, not just this niche: a partial read
            # means the list a human is about to approve is incomplete,
            # and there is no undo on the deletes it would authorise.
            print(f"ABORTING: {e}")
            raise SystemExit(1)
        all_matches[niche_name] = (table_name, matches, scanned)

    print("\n--- Matches found ---")
    total = 0
    for niche_name, (table_name, matches, scanned) in all_matches.items():
        for m in matches:
            print(f"[{niche_name}] {m['channel_name']} (@{m['handle']}) -> already in '{m['matched_in']}'")
            total += 1
    print(f"\nTotal matches: {total}")

    if not args.confirm:
        print("\nDry run only — no records deleted. Re-run with --confirm to delete these.")
        return

    # Circuit breaker, checked AFTER printing the list (so the human can
    # read what tripped it) and BEFORE the first delete_record() call.
    if not args.yes_delete_many:
        oversized = [
            f'"{niche_name}": {len(matches)} of {scanned} row(s)'
            for niche_name, (_table, matches, scanned) in all_matches.items()
            if len(matches) > DELETE_CAP_WITHOUT_OPT_IN
            or (scanned and len(matches) / scanned > DELETE_FRACTION_WITHOUT_OPT_IN)
        ]
        if oversized:
            print(
                "\nABORTING: this would delete an unexpectedly large share of a table — "
                + "; ".join(oversized)
                + f". The bounds are {DELETE_CAP_WITHOUT_OPT_IN} rows or "
                f"{DELETE_FRACTION_WITHOUT_OPT_IN:.0%} of a table, whichever trips first. "
                "Check the external handle index (external_handles_cache.json) for a bad "
                "entry before assuming the matches are real, then re-run with "
                "--yes-delete-many if they are. Deletions are permanent."
            )
            raise SystemExit(1)

    deleted = 0
    for niche_name, (table_name, matches, scanned) in all_matches.items():
        for m in matches:
            # A blank record_id would turn delete_record()'s URL into the
            # TABLE endpoint rather than a record endpoint. Airtable would
            # reject that today, but "we send a DELETE at the collection
            # instead of the row" is not a request this script should ever
            # be one API change away from making.
            if not m["record_id"]:
                print(f"[{niche_name}] [SKIPPED: no record id] {m['channel_name']}")
                continue
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
