"""
One-off / repeatable: check tracked rows for the no-social drop.

WHY THIS IS SEPARATE FROM audit_prospects.py. That script re-checks every
criterion that can be answered from the YouTube Data API, and deliberately runs
without a browser — which means it cannot see the one signal this gate needs.
`main.DROP_NO_SOCIAL` fires on an EMPTY About link list, and the link list only
exists in the rendered page (`channels.list` does not expose links at all, which
is the whole reason browser_email.py exists). So a row can pass the full audit
and still have no outreach surface beyond YouTube.

WHY THE BACKLOG EXISTS. Until 2026-08-15 the no-social drop was unreachable for
any channel whose email came from chain steps 1-4: the link-presence flag was
only produced by step 5, and step 5 only ran when every earlier step missed.
Since essentially every row carries an email — and the most common source is an
address repeated in the video descriptions, which is step 1 — the gate almost
never ran. Every row written before that fix has therefore never been checked.

Costs NO quota and NO influencers.club credits: one browser page load per row,
reading a page that is already public. Slow (a few seconds a row), not expensive.

    python audit_no_social.py                         # report only
    python audit_no_social.py --confirm --only "Name"  # delete, allowlisted
"""
import argparse
import logging
import time

from airtable_client import delete_record, get_records
from browser_email import BrowserEmailScraper
from config import API_SLEEP_SECONDS
from main import DROP_NO_SOCIAL, NICHES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Same contract as audit_prospects.py: only rows no human has ruled on may be
# deleted. An Approved/Rejected row carries a reviewer's decision, and this
# script is not entitled to overturn it.
DELETABLE_STATUSES = {"New"}

# The mass-delete circuit breaker, matching cleanup_external_duplicates.py.
# Both bounds exist because either alone has a blind spot: a fraction misses
# tiny tables, an absolute cap misses large ones.
MAX_DELETES = 25
MAX_DELETE_FRACTION = 0.5


def sweep_table(scraper, niche_name: str, table_name: str) -> list[dict]:
    """Read every row's link list. Returns the rows that have none."""
    if not table_name:
        logger.error("No table configured for niche '%s' — skipping.", niche_name)
        return []

    records = get_records(
        table_name, fields=["Channel ID", "Channel Name", "Status", "Date Added"]
    )
    logger.info("'%s': checking %d row(s).", niche_name, len(records))

    findings = []
    for i, record in enumerate(records, start=1):
        fields = record["fields"]
        channel_id = fields.get("Channel ID")
        name = (fields.get("Channel Name") or "")[:36]
        if not channel_id:
            continue

        # need_email=False: the link list is the whole question here, so none of
        # the link-following navigations are worth paying for.
        _, has_links = scraper.find_contact(channel_id, need_email=False)

        # Only a positively-EMPTY list counts. None means the page never
        # rendered or the About panel was absent — absent data, which never
        # disqualifies, the same rule the pipeline itself applies.
        if has_links is False:
            findings.append({
                "record_id": record["id"],
                "name": fields.get("Channel Name") or "",
                "status": fields.get("Status") or "(unset)",
                "added": fields.get("Date Added") or "",
                "deletable": (fields.get("Status") or "") in DELETABLE_STATUSES,
            })
            print(f"  {i:>3}/{len(records)} EMPTY   {name:<36} "
                  f"[{fields.get('Status')}] added {fields.get('Date Added')}")
        elif has_links is None:
            print(f"  {i:>3}/{len(records)} unknown {name:<36} (page unreadable — kept)")
        time.sleep(API_SLEEP_SECONDS)

    return findings


def delete_findings(table_name: str, findings: list[dict], total_rows: int,
                    yes_delete_many: bool, only_names: set[str] | None) -> int:
    """Delete the deletable findings, subject to the allowlist and the breaker."""
    targets = [f for f in findings if f["deletable"]]
    if only_names is not None:
        skipped = [f for f in targets if f["name"] not in only_names]
        targets = [f for f in targets if f["name"] in only_names]
        for f in skipped:
            print(f"SKIP (not in --only) {f['name']} [{f['status']}]")
    if not targets:
        return 0

    fraction = len(targets) / total_rows if total_rows else 0
    if not yes_delete_many and (len(targets) > MAX_DELETES or fraction > MAX_DELETE_FRACTION):
        print(
            f"\nREFUSING to delete {len(targets)} of {total_rows} rows "
            f"({fraction:.0%}) — over the circuit breaker "
            f"({MAX_DELETES} rows or {MAX_DELETE_FRACTION:.0%}).\n"
            "That many empty link lists is more likely a browser that never "
            "rendered than a real result. Re-read the report above."
        )
        return 0

    deleted = 0
    for target in targets:
        if delete_record(table_name, target["record_id"]):
            deleted += 1
            print(f"DELETED {target['name']} ({DROP_NO_SOCIAL})")
        else:
            print(f"FAILED to delete {target['name']} — left in place")
        time.sleep(API_SLEEP_SECONDS)
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find tracked rows whose channel has no external links"
    )
    parser.add_argument("--niche", default=None, help="Only this niche (default: all)")
    parser.add_argument(
        "--confirm", action="store_true",
        help="Actually DELETE failing rows whose Status is New. Without this, report only.",
    )
    parser.add_argument(
        "--yes-delete-many", action="store_true",
        help=f"Override the circuit breaker (>{MAX_DELETES} rows or >{MAX_DELETE_FRACTION:.0%} of a table).",
    )
    parser.add_argument(
        "--only", action="append", metavar="CHANNEL_NAME",
        help="Repeatable allowlist: delete ONLY these Channel Names, and only if "
             "the sweep also finds them empty. Use this to act on a report a "
             "human approved — a creator can add a link between the report and "
             "the deletion, and this closes that gap.",
    )
    args = parser.parse_args()

    if args.niche and args.niche not in NICHES:
        parser.error(f"Unknown niche {args.niche!r}. Known: {', '.join(NICHES)}")
    niches = {args.niche: NICHES[args.niche]} if args.niche else dict(NICHES)

    scraper = BrowserEmailScraper.launch()
    if not scraper.enabled:
        # Fails LOUD rather than reporting a clean sweep. An inert scraper
        # returns None for every channel, which reads as "nothing found" —
        # exactly the false all-clear this script exists to prevent.
        logger.error(
            "The browser did not start, so every link list would read as "
            "unknown and this sweep would report a false all-clear. Install "
            "the Playwright browser (`playwright install chromium`) and re-run."
        )
        raise SystemExit(1)

    print(f"Mode: {'DELETE' if args.confirm else 'REPORT-ONLY'}. "
          f"Deletable statuses: {sorted(DELETABLE_STATUSES)}\n")

    only_names = set(args.only) if args.only else None
    grand_found = 0
    grand_deleted = 0
    try:
        for niche_name, config in niches.items():
            table_name = config.get("table_name", "")
            print(f"=== {niche_name} ===")
            findings = sweep_table(scraper, niche_name, table_name)
            grand_found += len(findings)
            if args.confirm and findings:
                total = len(get_records(table_name, fields=["Channel ID"]))
                grand_deleted += delete_findings(
                    table_name, findings, total, args.yes_delete_many, only_names
                )
    finally:
        scraper.close()

    print(f"\n=== {grand_found} row(s) with no external links, {grand_deleted} deleted ===")
    if not args.confirm and grand_found:
        print("Re-run with --confirm (and --only NAME) to delete.")


if __name__ == "__main__":
    main()
