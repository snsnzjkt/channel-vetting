"""
Order the rows awaiting review, best-first, from the reviewer's own history.

Read-only. Costs nothing — no Gemini request, no vendor credit, no YouTube quota.
It reads the tables, learns each finding keyword's approval rate from rows the
reviewer has ALREADY judged, and prints the unjudged rows in that order.

This is an ORDERING, not a filter. Every pending row is printed; only the
sequence changes. Its worst case is the order we have today, which is arrival
order — that bound is why it can ship on a signal measured at AUC 0.602 with a
95% CI of [0.442, 0.749], i.e. promising and not yet proven. A gate on the same
signal could not ship, and 14.16 records the AI screen that failed to.

    python rank_pending.py                 # both niches
    python rank_pending.py --niche "Home Theater"
    python rank_pending.py --show-rates    # the keyword table it learned
"""
import argparse
from collections import Counter

import config
import ranking
from airtable_client import get_records

FIELDS = ["Channel ID", "Channel Name", "Status", "Source", "Subscriber Count",
          "Overall Score"]

NICHE_TABLES = (
    ("Home Theater", config.AIRTABLE_TABLE_HOME_THEATER),
    ("Lifestyle Sofa", config.AIRTABLE_TABLE_LIFESTYLE_SOFA),
)


def load(table):
    rows = []
    for rec in get_records(table, fields=FIELDS):
        f = rec.get("fields", {})
        if not f.get("Channel ID"):
            continue
        rows.append({
            "name": f.get("Channel Name", "?"),
            "source": f.get("Source", ""),
            "label": f.get("Status"),
            "subs": f.get("Subscriber Count"),
            "score": f.get("Overall Score"),
        })
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--niche", help="only this niche")
    ap.add_argument("--show-rates", action="store_true",
                    help="print the learned per-keyword table")
    ap.add_argument("--top", type=int, default=0, help="print only the first N")
    args = ap.parse_args(argv)

    for niche, table in NICHE_TABLES:
        if not table or (args.niche and args.niche.lower() not in niche.lower()):
            continue
        rows = load(table)
        judged = [r for r in rows if r["label"] in ("Approved", "Rejected")]
        pending = [r for r in rows if r["label"] not in ("Approved", "Rejected")]
        rates = ranking.approval_rates(judged)

        counts = Counter(r["label"] for r in judged)
        print(f"\n{'=' * 74}")
        print(f"{niche}: {len(pending)} awaiting review, "
              f"learned from {len(judged)} judged "
              f"({counts['Approved']}A / {counts['Rejected']}R)")
        print("=" * 74)

        if args.show_rates:
            usable = {k: v for k, v in rates.items() if v["rate"] is not None}
            thin = len(rates) - len(usable)
            print(f"\n  keyword table ({len(usable)} usable, {thin} too thin at "
                  f"<{ranking.MIN_LABELLED_FOR_RATE} verdicts):")
            for k, v in sorted(usable.items(), key=lambda kv: -kv[1]["rate"]):
                n = v["approved"] + v["rejected"]
                print(f"    {v['rate']:5.0%}  {v['approved']}/{n}  {k}")

        if not pending:
            print("\n  nothing pending.")
            continue

        ordered = ranking.rank(pending, rates)
        shown = ordered[:args.top] if args.top else ordered
        print(f"\n  {'#':>3}  {'priority':>8}  {'channel':32s} why")
        for i, r in enumerate(shown, 1):
            print(f"  {i:3d}  {r['priority']:8.2f}  {r['name'][:32]:32s} "
                  f"{r['priority_reason']}")

        # Two DIFFERENT questions, and reporting only the first was misleading.
        #
        #   1. How many rows could be scored at all?
        #   2. Is there any SPREAD between the scores?
        #
        # A niche whose every pending row came from one source scores 31 of 31
        # "rankable" and is still a flat list in arrival order. That reads as an
        # informative ranking and is not one. The rate itself may still be the
        # useful output — knowing the vendor path converts at 51% is worth having
        # even when it orders nothing — so say which of the two you got.
        scored = [r for r in ordered if r["priority"] != ranking.NEUTRAL_PRIOR]
        spread = (max(r["priority"] for r in ordered)
                  - min(r["priority"] for r in ordered)) if ordered else 0.0
        buckets = len({r["priority_reason"] for r in ordered})
        print(f"\n  {len(scored)} of {len(ordered)} rows scored on real history; "
              f"{buckets} distinct source bucket(s); spread {spread:.2f}")
        if not scored:
            print("  -> NO information: every pending row fell back to the "
                  "neutral prior.")
        elif spread < 0.05:
            print("  -> the ORDER here is uninformative: every row scores about "
                  "the same, so this is arrival order with a number attached. "
                  "The per-source RATE above is the useful output, not the "
                  "sequence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
