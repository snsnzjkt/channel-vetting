#!/usr/bin/env python3
"""
Write an already-consumed discovery HANDLE count into the credit ledger.

**Why this has to exist.** The handle allowance is enforced against
`credit_log.json`, and that ledger has never recorded handles — the meter was
invisible until the vendor mailed on 2026-09-01 to say we had used 5,042 of
5,000. So on the first run after the cap ships, the ledger honestly reports 0
handles used, and the cap cheerfully authorises a SECOND full allowance on top
of a period that is already over. A cap that starts from zero on an
already-spent period is worse than no cap, because it looks like protection.

This seeds the ledger with what the vendor says we have already spent, so the
first enforced run starts from the truth.

    python scripts/backfill/seed_discovery_handles.py --handles 5042

By default it writes to TODAY, which is the conservative reading: the spend
happened at some unknown point in the current period, and dating it today keeps
it inside the window longest. Pass --date to place it accurately once the real
period start is known.

Idempotent by refusing, not by overwriting: run twice and the second run stops
rather than doubling the seed. `--force` overrides, for correcting a wrong
figure.
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from channel_vetting.budget import credit_tracker  # noqa: E402
from channel_vetting.config import (  # noqa: E402
    INFLUENCERS_HANDLE_PERIOD_DAYS,
    INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_PERIOD,
)

# The seed is tagged in `by_kind` so it is never mistaken for metered spend when
# someone reads the ledger by hand. It carries 0 credits on purpose: the money
# was already billed and recorded (or lost with a cache eviction) elsewhere, and
# adding it again here would double-count the credit meter to fix the handle one.
SEED_MARKER = "seed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--handles", type=int, required=True,
        help="Handles already consumed this period, per the vendor.",
    )
    parser.add_argument(
        "--date", default=None,
        help="YYYY-MM-DD to attribute the spend to (default: today).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing seed instead of refusing.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would change and write nothing.",
    )
    args = parser.parse_args()

    if args.handles < 0:
        print("--handles cannot be negative.", file=sys.stderr)
        return 2

    day = args.date or date.today().isoformat()
    try:
        date.fromisoformat(day)
    except ValueError:
        print(f"--date {day!r} is not YYYY-MM-DD.", file=sys.stderr)
        return 2

    try:
        log = credit_tracker.load_log()
    except credit_tracker.CreditLedgerUnavailable as exc:
        print(f"Refusing to seed: {exc}", file=sys.stderr)
        return 1

    entry = log["days"].setdefault(day, {"total": 0.0, "by_kind": {}})
    existing = int(entry.get("handles", 0) or 0)
    already_seeded = SEED_MARKER in entry.get("by_kind", {})

    if already_seeded and not args.force:
        print(
            f"{day} already carries a seed of {existing} handles. Re-running "
            f"would double it. Pass --force to replace the figure.",
            file=sys.stderr,
        )
        return 1

    before = credit_tracker._handles_in_window(log)
    entry["handles"] = args.handles if already_seeded else existing + args.handles
    entry.setdefault("by_kind", {})[SEED_MARKER] = 0.0
    after = credit_tracker._handles_in_window(log)

    verb = "Would set" if args.dry_run else "Set"
    print(f"{verb} {day} handles to {entry['handles']} (was {existing}).")
    print(
        f"Handles in the counted period: {before} -> {after} of "
        f"{INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_PERIOD} "
        f"(window {INFLUENCERS_HANDLE_PERIOD_DAYS}d)."
    )
    if after >= INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_PERIOD:
        print(
            "\nThis puts the account AT OR OVER the allowance, so paid discovery "
            "will now decline to buy pages until the period rolls. That is the "
            "intended result of seeding an already-exceeded period. Niches on "
            "discovery_source=both keep running on the free YouTube keyword loop."
        )

    if args.dry_run:
        return 0

    try:
        credit_tracker._save_log(log)
    except (OSError, credit_tracker.CreditLedgerUnavailable) as exc:
        print(f"Could not write the ledger: {exc}", file=sys.stderr)
        return 1
    print(f"\nWrote {credit_tracker.CREDIT_LOG_FILE}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
