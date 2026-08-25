#!/usr/bin/env python3
"""
Categorise the legacy outreach population into review lists. FREE — no money.

    python scripts/followup/categorize.py                 # dry run, writes nothing
    python scripts/followup/categorize.py --handle @foo   # explain ONE row
    python scripts/followup/categorize.py --confirm       # write to Airtable

DRY RUN IS THE DEFAULT, and on the largest-blast-radius write in this repo that
is not a nicety. Every comparable script here is read-only until told otherwise:
`audit_blocklist.py` needs --mark, `cleanup_external_duplicates.py` needs
--confirm, `find_external_duplicates.py` needs --delete AND --yes. A dry run
prints exactly the histogram it would write, so the safe path and the useful
path are the same keystroke.

WHAT IT COSTS: nothing. Airtable reads and writes only — no YouTube, no model,
no vendor credits. The activity signal is read from the sweep's local cache
rather than re-fetched.
"""
import argparse
import logging
import sys
from collections import Counter
from datetime import datetime, timezone

from channel_vetting.airtable.client import get_records
from channel_vetting.airtable.do_not_contact import fetch_blocklist
from channel_vetting.config import OUTREACH_MAX_TOUCHES, OUTREACH_RESPAM_MIN_DAYS
from channel_vetting.followup import categorizer as C
from channel_vetting.followup import legacy
from channel_vetting.followup import writer as W

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

EXIT_OK, EXIT_ABORTED, EXIT_NOTHING_DONE = 0, 1, 2

F_CATEGORY = "Follow-Up Category"
F_REASON = "Follow-Up Reason"
F_AT = "Categorized At"
F_CHANNEL_ID = "Channel ID"
F_ALIVE = "Channel Alive"
F_CHECKED = "Activity Checked At"


def main() -> int:
    ap = argparse.ArgumentParser(description="Categorise the legacy follow-up population (free).")
    ap.add_argument("--confirm", action="store_true", help="write to Airtable (default: dry run)")
    ap.add_argument("--handle", help="explain ONE handle and exit; writes nothing")
    ap.add_argument("--refresh-population", action="store_true")
    ap.add_argument("--sample", type=int, default=0,
                    help="print N example rows per category, for eyeballing reasons")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)

    print("=" * 74)
    print("FOLLOW-UP CATEGORISER" + ("" if args.confirm else "   [DRY RUN — writes nothing]"))
    print(f"floor {OUTREACH_RESPAM_MIN_DAYS}d · ceiling {OUTREACH_MAX_TOUCHES} touches")
    print("=" * 74)

    main_rows, follow_rows = legacy.load_population(refresh=args.refresh_population)
    bl = fetch_blocklist()

    # Activity comes from the sweep's cache. A handle ABSENT from it is absent
    # from this dict, which routes it to an Unknown bucket rather than to a
    # verdict — "not checked" must never render as "checked and fine".
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from sweep_activity import load_activity_verdicts, load_cache
    activity = load_activity_verdicts()
    sweep_cache = load_cache()
    print(f"\nactivity cache: {len(activity)} of {len({r.handle for r in main_rows if r.handle})} "
          f"handles checked")

    # Existing categories, for the TERMINAL latch. Without this a half-failed
    # read on a later run could demote someone already filed as suppressed or
    # replied back toward eligible — idempotence is not the property that
    # matters here, monotonicity is.
    prev_rows = get_records(legacy.TABLE_MAIN, [F_CATEGORY])
    previous_by_rec = {r["id"]: (r["fields"] or {}).get(F_CATEGORY, "") for r in prev_rows}
    handle_of_rec = {r.record_id: (r.handle or f"rec:{r.record_id}") for r in main_rows}
    previous = {}
    for rec, cat in previous_by_rec.items():
        if cat and rec in handle_of_rec:
            previous[handle_of_rec[rec]] = cat
    print(f"already categorised: {len(previous)} handles")

    cats = legacy.categorize_population(
        main_rows, follow_rows, bl, now=now,
        floor_days=OUTREACH_RESPAM_MIN_DAYS, max_touches=OUTREACH_MAX_TOUCHES,
        activity=activity, previous=previous)

    if args.handle:
        h = args.handle.lstrip("@").lower()
        if h not in cats:
            print(f"\n@{h} is not in the legacy population.")
            return EXIT_NOTHING_DONE
        cat, reason = cats[h]
        rows = [r for r in main_rows if r.handle == h]
        print(f"\n   handle          @{h}")
        print(f"   rows            {len(rows)}  (a handle can appear more than once)")
        for r in rows[:3]:
            print(f"     {r.record_id}  {r.name!r}  {r.date}  mail_sent={r.mail_sent}")
        print(f"   DNC             {bl.match(handle=h, email=rows[0].email, name=rows[0].name) or '-'}")
        print(f"   activity        {activity.get(h, 'not checked')}")
        print(f"   CATEGORY        {cat}")
        print(f"   REASON          {reason}")
        print("\n   (writes nothing)")
        return EXIT_OK

    counts = Counter(c for c, _ in cats.values())
    print(f"\nCATEGORIES  ({len(main_rows)} rows -> {len(cats)} handles)")
    for cat in C.CATEGORIES:
        n = counts.get(cat, 0)
        flag = "  <- actionable" if cat == C.ACTIONABLE and n else ""
        print(f"   {cat:<22} {n:>6}{flag}")
    total = sum(counts.values())
    print(f"   {'':22} {'-'*6}\n   {'TOTAL':<22} {total:>6}")
    if total != len(cats):
        print("COVERAGE BUG: a handle fell out of every bucket.")
        return EXIT_ABORTED
    print("   coverage invariant holds (every handle is in exactly one bucket)")

    churn = sum(1 for h, (c, _) in cats.items()
                if h in previous and previous[h] != c)
    print(f"\n   category churn since last run: {churn}"
          + ("   <- non-zero with no new signal is the miscalibration alarm" if churn else ""))

    if args.sample:
        print(f"\nSAMPLE REASONS ({args.sample} per category)")
        for cat in C.CATEGORIES:
            ex = [(h, r) for h, (c, r) in cats.items() if c == cat][:args.sample]
            if ex:
                print(f"   {cat}:")
                for h, r in ex:
                    print(f"     @{h}: {r[:96]}")

    # Build the per-record payload. Every row for a handle gets the SAME verdict.
    updates: dict[str, dict] = {}
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in main_rows:
        key = r.handle or f"rec:{r.record_id}"
        cat, reason = cats[key]
        fields = {F_CATEGORY: cat, F_REASON: reason, F_AT: stamp}
        probe = sweep_cache.get(r.handle) if r.handle else None
        if probe:
            if probe.get("channel_id"):
                fields[F_CHANNEL_ID] = probe["channel_id"]
            fields[F_ALIVE] = probe.get("verdict") == "alive"
            if probe.get("checked_at"):
                fields[F_CHECKED] = probe["checked_at"]
        updates[r.record_id] = fields

    print(f"\n   {len(updates)} rows would be written "
          f"({(len(updates) + W.BATCH_SIZE - 1) // W.BATCH_SIZE} batched requests)")

    if not args.confirm:
        print("\n[dry run] nothing written. Add --confirm to write.")
        return EXIT_OK

    try:
        W.validate_categories(updates)
    except W.CategoryVocabularyError as e:
        print(f"\nREFUSED before any request: {e}")
        return EXIT_ABORTED

    written, failed = W.patch_records(legacy.TABLE_MAIN, updates)
    print(f"\nRUN SUMMARY")
    print(f"   examined {len(updates)}")
    print(f"   written  {written}")
    print(f"   failed   {len(failed)}")
    if failed:
        print(f"   first failed ids: {failed[:5]}")
    # Exit non-zero when rows were examined and none were written. This is the
    # "zero rows, exit code 0" hole docs/TODOS.md names, closed locally.
    if written == 0:
        print("\nNOTHING WAS WRITTEN. Not reporting success.")
        return EXIT_NOTHING_DONE
    if failed:
        print("\nPartial write. Re-run to retry the failures (it is idempotent).")
        return EXIT_ABORTED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
