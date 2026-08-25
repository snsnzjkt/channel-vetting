"""
Rows in the niche tables that are already tracked in the base's other tables.

REPORT-ONLY by default. `--delete` exists but is deliberately awkward: it also
requires `--yes`, and it REFUSES to touch a row you have already judged.

## Why judged rows are refused

An Approved or Rejected row carries a reviewer verdict, and verdicts are the
scarcest thing in this project — the whole backtest corpus is 5 usable rows
(YIELD_OPTIMIZATION_PLAN.md 14.20). Deleting one destroys evidence that cannot be
regenerated, to save a duplicate that costs nothing now it is already paid for.
Use `--include-judged` to override, and think about it first.

## Why these exist at all

`fetch_external_handles` cached the dedupe index for up to 24 hours while the
team edits those outreach tables continuously, so a run could not see a channel
added externally that morning. Fixed at the call site in `pipeline.run` — the index
is now rebuilt every run. This script cleans up what got through before that.

    python scripts/audit/find_external_duplicates.py                 # report
    python scripts/audit/find_external_duplicates.py --delete --yes  # act, unjudged only
"""
import argparse
import sys

from channel_vetting import config
from channel_vetting.enrichment import external_dedupe as ed
from channel_vetting.airtable.client import get_records

NICHES = (("Home Theater", config.AIRTABLE_TABLE_HOME_THEATER),
          ("Lifestyle Sofa", config.AIRTABLE_TABLE_LIFESTYLE_SOFA))
JUDGED = ("Approved", "Rejected")


def find(index):
    """[(niche, table, record_id, name, status, date, source_table)] for every dupe."""
    out = []
    for niche, table in NICHES:
        if not table:
            continue
        for rec in get_records(table, fields=["Channel Name", "Handle", "Status",
                                              "Date Added"]):
            f = rec.get("fields", {})
            src = index.match(handle=f.get("Handle", ""),
                              name=f.get("Channel Name", ""))
            if src:
                out.append((niche, table, rec["id"], f.get("Channel Name", "?"),
                            f.get("Status") or "New",
                            (f.get("Date Added") or "?")[:10], src))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--delete", action="store_true", help="actually remove rows")
    ap.add_argument("--yes", action="store_true", help="required alongside --delete")
    ap.add_argument("--include-judged", action="store_true",
                    help="also delete Approved/Rejected rows (destroys verdicts)")
    args = ap.parse_args(argv)

    print("Rebuilding the external index (~90s, ~18k rows over 6 tables)...")
    index = ed.fetch_external_handles(force_refresh=True)
    dupes = find(index)

    if not dupes:
        print("\nNo duplicates. Nothing to do.")
        return 0

    unjudged = [d for d in dupes if d[4] not in JUDGED]
    judged = [d for d in dupes if d[4] in JUDGED]

    print(f"\n{len(dupes)} row(s) already tracked elsewhere "
          f"({len(unjudged)} unjudged, {len(judged)} carrying a verdict)\n")
    for niche, _, _, name, status, date, src in sorted(dupes):
        flag = "  <-- HAS A VERDICT" if status in JUDGED else ""
        print(f"  {niche[:2]}  {date}  [{status:8s}] {name[:34]:34s} <- {src}{flag}")

    if not args.delete:
        print(f"\nReport only. To remove the {len(unjudged)} unjudged row(s):")
        print("  python scripts/audit/find_external_duplicates.py --delete --yes")
        if judged:
            print(f"\nThe {len(judged)} row(s) marked above carry a reviewer verdict "
                  f"and are REFUSED by default.\nVerdicts are the scarcest thing "
                  f"here — the backtest corpus is 5 usable rows. Deleting one\n"
                  f"destroys evidence to save a duplicate that costs nothing now. "
                  f"--include-judged overrides.")
        return 0

    if not args.yes:
        print("\n--delete requires --yes as well. Nothing was changed.")
        return 1

    targets = dupes if args.include_judged else unjudged
    if args.include_judged and judged:
        print(f"\n--include-judged: {len(judged)} verdict(s) will be destroyed.")

    # Imported here, not at module scope: the REPORT path must not be able to
    # reach a delete helper at all.
    from channel_vetting.airtable.client import delete_record

    print(f"\nDeleting {len(targets)} row(s). This is permanent — Airtable's API "
          f"has no undo on our end.\n")
    removed, failed = 0, []
    for niche, table, rec_id, name, status, _, _ in targets:
        if delete_record(table, rec_id):
            removed += 1
            print(f"  removed  [{status:8s}] {name[:40]}")
        else:
            failed.append(name)
            print(f"  FAILED   [{status:8s}] {name[:40]}")
    print(f"\nRemoved {removed} of {len(targets)}. "
          f"{len(dupes) - len(targets)} left in place.")
    if failed:
        print(f"{len(failed)} failed and are still present: {', '.join(failed[:5])}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
