#!/usr/bin/env python3
"""
Activity sweep for the legacy follow-up population. FREE — no money, ever.

    # what it would do, no API calls at all
    python scripts/followup/sweep_activity.py

    # one channel, prints every input signal, writes nothing
    python scripts/followup/sweep_activity.py --handle @somechannel

    # spend free YouTube quota, bounded by the discovery reserve
    python scripts/followup/sweep_activity.py --confirm

DRY-RUN IS THE DEFAULT, matching every other write-capable script in this repo
(`audit_blocklist.py` is read-only unless --mark; `cleanup_external_duplicates.py`
requires --confirm; `find_external_duplicates.py` requires --delete --yes).

WHAT IT COSTS
-------------
Nothing, in money. It calls exactly two YouTube Data API endpoints, whose quota
is a free daily allowance (10,000 units) and a rate limit — there is no per-unit
billing and no way to buy units. `budget/credit_tracker.py` states the repo's
only real-money surface is influencers.club credits, which this never touches.
`activity.assert_free_only()` proves that per run rather than asserting it.

The real constraint is that discovery spends from the same free allowance.
FOLLOWUP_ACTIVITY_QUOTA_RESERVE holds back the MEASURED peak discovery day
(5,400 units), so this sweep gets ~2,600/day and a full first pass over the
9,991 survivors takes ~8 days. That is elapsed time on a free resource, not a bill.

RESUMABILITY
------------
Results are keyed by handle in a local JSON cache, so a killed run resumes at
the next unprobed handle rather than starting over. The selection is "handles
with no cached result", NOT an Airtable date filter — `outreach_store.py:186-199`
records the measurement that `{field} != BLANK()` matches an EMPTY dateTime
(47 of 47 rows), so a BLANK()-based cursor would re-sweep everything forever.
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

from channel_vetting.airtable.do_not_contact import fetch_blocklist
from channel_vetting.budget import quota_tracker
from channel_vetting.config import (
    API_SLEEP_SECONDS,
    FOLLOWUP_ACTIVITY_CACHE as _ACT_CACHE,
    FOLLOWUP_ACTIVITY_CHANNELS_PER_RUN,
    FOLLOWUP_ACTIVITY_FAILURE_BREAKER,
    FOLLOWUP_ACTIVITY_QUOTA_RESERVE,
    FOLLOWUP_INACTIVE_MAX_DAYS,
    OUTREACH_RESPAM_MIN_DAYS,
    QUOTA_CEILING,
)
from channel_vetting.enrichment.external_dedupe import _normalize_name
from channel_vetting.followup import activity as A
from channel_vetting.followup import legacy

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sweep")

# data_path() returns a str.
FOLLOWUP_ACTIVITY_CACHE = Path(_ACT_CACHE)

# Exit codes, borrowed from outreach/sender.py rather than invented, so a CI
# step reads the same contract everywhere.
EXIT_OK = 0
EXIT_ABORTED = 1
EXIT_NOTHING_DONE = 2


def load_activity_verdicts() -> dict:
    """
    handle -> True/False from the sweep cache. A handle ABSENT from the cache is
    absent from this dict, which is what routes it to Activity Unknown rather
    than to a verdict — the distinction between "checked and fine" and "not
    checked" is the whole reason the Unknown buckets exist.
    """
    c = load_cache()
    return {h: (v.get("verdict") == "alive") for h, v in c.items()
            if v.get("verdict") in ("alive", "gone")}


def load_cache() -> dict:
    if FOLLOWUP_ACTIVITY_CACHE.exists():
        return json.loads(FOLLOWUP_ACTIVITY_CACHE.read_text())
    return {}


def save_cache(cache: dict) -> None:
    """
    Atomic replace. A plain open(...,'w') truncates first, so a Ctrl-C mid-dump
    leaves a half-written document — the same failure quota_tracker._save_log()
    hardens against, and here it would throw away hours of free-quota work.
    """
    tmp = FOLLOWUP_ACTIVITY_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(FOLLOWUP_ACTIVITY_CACHE)


def main() -> int:
    ap = argparse.ArgumentParser(description="Free activity sweep for the legacy follow-up population.")
    ap.add_argument("--confirm", action="store_true", help="actually call YouTube (default: dry run)")
    ap.add_argument("--full-activity", action="store_true",
                    help="2-unit sweep incl. upload age. Default is the 1-unit "
                         "dead-channel check, chosen on measurement: channel_gone "
                         "is 8.0%% of the population for the first unit, upload-age "
                         "inactivity only 3.7%% for the second.")
    ap.add_argument("--handle", help="probe ONE handle and exit; writes nothing")
    ap.add_argument("--limit", type=int, default=FOLLOWUP_ACTIVITY_CHANNELS_PER_RUN)
    ap.add_argument("--reserve", type=int, default=FOLLOWUP_ACTIVITY_QUOTA_RESERVE,
                    help="free units held back for discovery")
    ap.add_argument("--refresh-population", action="store_true", help="re-read Airtable")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)

    print("=" * 72)
    print("FOLLOW-UP ACTIVITY SWEEP" + ("" if args.confirm else "   [DRY RUN — no API calls]"))
    print("=" * 72)

    passed, warnings = A.assert_free_only()
    print("\nFREE-ONLY PREFLIGHT")
    for c in passed:
        print(f"   PASS  {c}")
    for w in warnings:
        print(f"   WARN  {w}")

    units_each = A.UNITS_FIRST_PASS if args.full_activity else A.UNITS_DEAD_ONLY
    mode = "2-unit full activity" if args.full_activity else "1-unit dead-channel"
    spent = quota_tracker.get_today_spend()
    budget = max(0, QUOTA_CEILING - spent - args.reserve)
    affordable = budget // units_each
    print(f"\nFREE QUOTA (no monetary charge)")
    print(f"   ceiling {QUOTA_CEILING} | spent today {spent} | discovery reserve {args.reserve}")
    print(f"   mode: {mode}")
    print(f"   sweep budget {budget} units -> {affordable} channels @ {units_each}u")
    print(f"   hard per-run cap (ignores the quota log): {args.limit} channels")

    # --- single-handle probe: the verify_video.py pattern ---------------------
    if args.handle:
        h = args.handle.lstrip("@").lower()
        if not args.confirm:
            print(f"\n[dry run] would probe @{h} for {units_each} free unit(s). "
                  f"Add --confirm to run it.")
            return EXIT_OK
        if not args.full_activity:
            ap2 = A.fetch_channel_alive(h)
            print(f"\n   handle        @{h}")
            print(f"   resolved id   {ap2.channel_id or '-'}")
            print(f"   channel title {ap2.channel_title or '-'}")
            print(f"   subscribers   {ap2.subscriber_count or '-'}")
            print(f"   VERDICT       {ap2.verdict}  ({ap2.reason})")
            print(f"   title changed {ap2.title_flag}  (advisory only, never an exclusion)")
            print(f"   free units    {ap2.units_spent}")
            return EXIT_OK
        p = A.fetch_last_upload(h)
        days = A.days_since_upload(p.last_upload_at, now) if p.ok else None
        verdict, reason = A.classify_activity(days, FOLLOWUP_INACTIVE_MAX_DAYS)
        print(f"\n   handle        @{h}")
        print(f"   resolved id   {p.channel_id or '-'}")
        print(f"   channel title {p.channel_title or '-'}")
        print(f"   last upload   {p.last_upload_at or '-'}")
        print(f"   days since    {days if days is not None else '-'}")
        print(f"   VERDICT       {verdict}  ({reason if p.ok else p.reason})")
        print(f"   free units    {p.units_spent}")
        return EXIT_OK

    # --- population + free screens ------------------------------------------
    main_rows, follow_rows = legacy.load_population(refresh=args.refresh_population)
    bl = fetch_blocklist()
    from collections import Counter
    from channel_vetting.config import OUTREACH_MAX_TOUCHES
    from channel_vetting.followup import categorizer as C

    cats = legacy.categorize_population(
        main_rows, follow_rows, bl, now=now,
        floor_days=OUTREACH_RESPAM_MIN_DAYS, max_touches=OUTREACH_MAX_TOUCHES,
        activity=load_activity_verdicts())

    counts = Counter(c for c, _ in cats.values())
    print(f"\nCATEGORIES  ({len(main_rows)} rows -> {len(cats)} handles, "
          f"{OUTREACH_RESPAM_MIN_DAYS}d floor)")
    for cat in C.CATEGORIES:
        print(f"   {cat:<22} {counts.get(cat, 0):>6}")
    assert sum(counts.values()) == len(cats), "coverage bug"
    print(f"   {'':22} {'-'*6}\n   {'TOTAL':<22} {sum(counts.values()):>6}   (invariant holds)")

    to_probe = legacy.needs_paid_signal(cats)
    by_handle = {h: [] for h in to_probe if not h.startswith("rec:")}
    print(f"\n   {len(to_probe)} handles still undecided -> need a paid signal")

    cache = load_cache()
    todo = [h for h in by_handle if h not in cache]
    print(f"   already probed (cache): {len(cache)} | remaining: {len(todo)}")

    n = min(len(todo), args.limit, affordable if args.confirm else args.limit)
    if args.confirm and affordable == 0:
        print("\nNo free budget left today after the discovery reserve. Resume tomorrow.")
        return EXIT_NOTHING_DONE
    print(f"\n   this run will probe {n} handles ({n * units_each} free units, {mode})")
    full_pass_units = len(todo) * units_each
    per_day = max(QUOTA_CEILING - args.reserve, 1)
    print(f"   full remaining pass: {full_pass_units} free units "
          f"(~{full_pass_units / per_day:.1f} days at {per_day} units/day after the reserve)")
    if not args.confirm:
        print("\n[dry run] nothing called, nothing written. Add --confirm to sweep.")
        return EXIT_OK

    # --- the sweep -----------------------------------------------------------
    probed = failures = consecutive = 0
    verdicts: dict[str, int] = {}
    mismatches = []
    try:
        for i, h in enumerate(todo[:n], 1):
            if not args.full_activity:
                ap2 = A.fetch_channel_alive(h, stored_name=by_handle[h][0].name)
                probed += 1
                if ap2.verdict == A.GONE:
                    failures += 1
                    consecutive += 1
                else:
                    consecutive = 0
                verdicts[ap2.verdict] = verdicts.get(ap2.verdict, 0) + 1
                if ap2.title_flag:
                    mismatches.append((h, by_handle[h][0].name, ap2.channel_title))
                cache[h] = {"channel_id": ap2.channel_id, "title": ap2.channel_title,
                            "subscribers": ap2.subscriber_count,
                            "verdict": ap2.verdict, "reason": ap2.reason,
                            "title_changed": ap2.title_flag,
                            "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
                if consecutive >= FOLLOWUP_ACTIVITY_FAILURE_BREAKER:
                    save_cache(cache)
                    print(f"\nBREAKER: {consecutive} consecutive misses at {i}/{n}. "
                          "A quota wall and a deleted channel are indistinguishable "
                          "through get_channel_stats(), so this halts rather than "
                          "mark thousands of live channels dead.")
                    return EXIT_ABORTED
                if i % 50 == 0:
                    save_cache(cache)
                    print(f"   {i}/{n} probed | {quota_tracker.get_today_spend()} free units spent today")
                time.sleep(API_SLEEP_SECONDS)
                continue
            p = A.fetch_last_upload(h)
            probed += 1
            if p.ok:
                consecutive = 0
                days = A.days_since_upload(p.last_upload_at, now)
                verdict, reason = A.classify_activity(days, FOLLOWUP_INACTIVE_MAX_DAYS)
                # Handle recycling: a 2024 @handle can belong to someone else by
                # 2026 (the repo's recorded case: @Newrecordday2013 ->
                # @newrecordday). A title mismatch means we measured the WRONG
                # channel, so it is Unresolvable, not a verdict.
                stored = _normalize_name(by_handle[h][0].name)
                live = _normalize_name(p.channel_title)
                if stored and live and stored != live:
                    verdict, reason = "unresolvable", f"title mismatch: stored '{by_handle[h][0].name}' vs live '{p.channel_title}'"
                    mismatches.append((h, by_handle[h][0].name, p.channel_title))
            else:
                failures += 1
                consecutive += 1
                days, verdict, reason = None, A.UNKNOWN, p.reason
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            cache[h] = {"channel_id": p.channel_id, "title": p.channel_title,
                        "last_upload_at": p.last_upload_at, "days": days,
                        "verdict": verdict, "reason": reason,
                        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}

            if consecutive >= FOLLOWUP_ACTIVITY_FAILURE_BREAKER:
                save_cache(cache)
                print(f"\nBREAKER: {consecutive} consecutive failures at {i}/{n}. "
                      "Likely a quota wall or auth failure, NOT thousands of dead "
                      "channels. Halting so nothing is mis-marked.")
                return EXIT_ABORTED
            if i % 50 == 0:
                save_cache(cache)
                print(f"   {i}/{n} probed | {quota_tracker.get_today_spend()} free units spent today")
            time.sleep(API_SLEEP_SECONDS)
    except KeyboardInterrupt:
        save_cache(cache)
        print("\nInterrupted. Progress saved — re-run to resume at the next unprobed handle.")
        return EXIT_ABORTED

    save_cache(cache)
    print(f"\nRUN SUMMARY  (printed every run, zeros included)")
    print(f"   probed        {probed}")
    print(f"   failures      {failures}")
    for v in ("alive", "gone", "active", "inactive", "unknown", "unresolvable"):
        print(f"   {v:<13} {verdicts.get(v, 0)}")
    print(f"   title CHANGED (advisory, not an exclusion): {len(mismatches)}")
    for h, s, l in mismatches[:5]:
        print(f"      @{h}: '{s}' -> '{l}'")
    print(f"   free units spent today: {quota_tracker.get_today_spend()}")
    print(f"   cache now holds {len(cache)} of {len(by_handle)} handles "
          f"({len(cache)/max(len(by_handle),1)*100:.1f}%)")

    if probed == 0:
        print("\nNothing probed.")
        return EXIT_NOTHING_DONE
    if probed and not verdicts:
        return EXIT_NOTHING_DONE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
