"""
Backtest the tier that actually DECIDES — the Gemini video tier — against labels.

## Why this tool exists

Every other relevance signal in this repo has been measured against
Status=Approved/Rejected, and four of them turned out anti-predictive. The video
tier never has been, and it is the only tier with authority: it alone can rescue
a candidate the keyword gate flagged.

`scripts/analysis/backtest_relevance.py` cannot do it. That script reads `gemini_cache.json` and
filters to `tier == "text"`, and the cache holds only VIDEO entries — so it joins
zero rows. Worse, video verdicts are keyed on the VIDEO id
(`video|model|VIDEO_ID|start|end|digest|version`) with no video->channel map
persisted, so the cache cannot be joined to a channel at all.

This reads the verdicts from AIRTABLE instead, where `push_record` writes
`Relevance State` / `Relevance Detail` / `Relevance Notes` alongside `Status`.
Zero new requests, zero credits: the verdicts were already paid for.

## What it measures

Does the tier's verdict predict the reviewer's? Two framings, because the tier
has two jobs:

  - CONFIRM vs NOT: does "video confirmed" land more on Approved than Rejected?
  - CONFIDENCE as a rank: does a higher number sort Approved above Rejected?

The prior to beat is the base rate. A tier that confirms everything has an
approval rate equal to the corpus and has told you nothing — which is what the
one existing hint suggests (6/6 Approved and 2/2 Rejected confirmed at the live
ratio, per GEMINI_VERIFY_PLAN.md).

    python scripts/analysis/backtest_video_tier.py
    python scripts/analysis/backtest_video_tier.py --min-rows 30   # refuse to report below n
"""
import argparse
import itertools
import random
import re
from collections import Counter

from channel_vetting import config
from channel_vetting.airtable.client import get_records

NICHE_TABLES = (
    ("Home Theater", config.AIRTABLE_TABLE_HOME_THEATER),
    ("Lifestyle Sofa", config.AIRTABLE_TABLE_LIFESTYLE_SOFA),
)

_CONF = re.compile(r"(\d\.\d{1,2})")
_RATIO = re.compile(r"(\d+)\s*/\s*(\d+)\s*criteria")


def parse_detail(detail: str) -> dict:
    """
    What `Relevance Detail` actually asserts.

    The field is prose assembled by `verification.gemini.judge`, so this reads it
    rather than a schema. The distinctions that matter:

      "video confirmed 0.90"                    -> confirmed, conf 0.90
      "video partly confirmed 0.85 (1/2 ...)"   -> confirmed, conf 0.85, 1 of 2
      "video did not confirm (0.90, 0/2 ...)"   -> not confirmed
      "failed a required criterion: ..."        -> not confirmed, a VETO
      "rescued (video confirmed 1.00)"          -> confirmed, and it acted
      "no long-form video to sample"            -> no verdict at all
      "unavailable (video_run_cap_reached)"     -> no verdict, budget artifact
    """
    text = (detail or "").strip()
    low = text.lower()
    out = {"confirmed": None, "confidence": None, "matched": None,
           "total": None, "veto": False, "no_verdict": False}
    if not text or "no long-form video" in low or low.startswith("unavailable"):
        out["no_verdict"] = True
        return out
    if "failed a required criterion" in low:
        out.update(confirmed=False, veto=True)
        return out
    if "did not confirm" in low:
        out["confirmed"] = False
    elif "confirmed" in low:          # covers "confirmed" and "partly confirmed"
        out["confirmed"] = True
    conf = _CONF.search(text)
    if conf:
        out["confidence"] = float(conf.group(1))
    ratio = _RATIO.search(text)
    if ratio:
        out["matched"], out["total"] = int(ratio.group(1)), int(ratio.group(2))
    return out


def auc(pairs) -> float | None:
    """P(a random Approved outranks a random Rejected). 0.50 is a coin flip."""
    app = [s for s, l in pairs if l == "Approved"]
    rej = [s for s, l in pairs if l == "Rejected"]
    if not app or not rej:
        return None
    wins = ties = 0
    for a, r in itertools.product(app, rej):
        if a > r:
            wins += 1
        elif a == r:
            ties += 1
    return (wins + 0.5 * ties) / (len(app) * len(rej))


def bootstrap_ci(pairs, n=2000):
    rnd = random.Random(4242)
    out = []
    for _ in range(n):
        sample = [rnd.choice(pairs) for _ in pairs]
        a = auc(sample)
        if a is not None:
            out.append(a)
    if not out:
        return None, None
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out))]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-rows", type=int, default=0,
                    help="warn loudly below this many joined rows")
    args = ap.parse_args(argv)

    joined, coverage = [], Counter()
    for niche, table in NICHE_TABLES:
        if not table:
            continue
        for rec in get_records(table):
            f = rec.get("fields", {})
            status = f.get("Status")
            state = f.get("Relevance State")
            coverage[f"{niche}: rows"] += 1
            if state:
                coverage[f"{niche}: has verdict"] += 1
            if status in ("Approved", "Rejected"):
                coverage[f"{niche}: has label"] += 1
            if not state or status not in ("Approved", "Rejected"):
                continue
            coverage[f"{niche}: JOINED"] += 1
            joined.append({"niche": niche, "label": status, "state": state,
                           "name": f.get("Channel Name", "?"),
                           **parse_detail(f.get("Relevance Detail", ""))})

    print("--- corpus ---")
    for k in sorted(coverage):
        print(f"  {k:32s} {coverage[k]}")

    usable = [r for r in joined if not r["no_verdict"] and r["confirmed"] is not None]
    print(f"\n  joined rows                      {len(joined)}")
    print(f"  of those, carrying a real verdict {len(usable)}"
          f"   ({len(joined) - len(usable)} were cap/no-video artifacts)")

    if not usable:
        print("\nNo row carries both a real verdict and a label. Nothing to measure.")
        print("The tier cannot be evaluated until judged rows also have verdicts —")
        print("i.e. until the reviewer labels rows the tier has already scored.")
        return 0

    base = Counter(r["label"] for r in usable)
    n = len(usable)
    base_rate = 100 * base["Approved"] / n
    print(f"\n--- discrimination (n={n}, base rate {base_rate:.0f}% Approved) ---")
    conf_rows = [r for r in usable if r["confirmed"]]
    deny_rows = [r for r in usable if not r["confirmed"]]
    for label, rows in (("CONFIRMED by the tier", conf_rows),
                        ("NOT confirmed", deny_rows)):
        if not rows:
            print(f"  {label:24s} n=0")
            continue
        app = sum(1 for r in rows if r["label"] == "Approved")
        print(f"  {label:24s} n={len(rows):3d}  {app} Approved "
              f"({100 * app / len(rows):.0f}%)  vs base {base_rate:.0f}%")
    if conf_rows and deny_rows:
        lift = (100 * sum(1 for r in conf_rows if r["label"] == "Approved") / len(conf_rows)
                - 100 * sum(1 for r in deny_rows if r["label"] == "Approved") / len(deny_rows))
        print(f"  -> lift from confirming: {lift:+.0f} percentage points")
    else:
        print("  -> the tier gave the SAME answer to every row: it cannot "
              "discriminate on this corpus, whatever its accuracy.")

    pairs = [(r["confidence"], r["label"]) for r in usable if r["confidence"] is not None]
    if pairs:
        a = auc(pairs)
        lo, hi = bootstrap_ci(pairs)
        if a is not None:
            print(f"\n--- confidence as a rank (n={len(pairs)}) ---")
            print(f"  AUC {a:.3f}   95% CI [{lo:.3f}, {hi:.3f}]")
            verdict = ("indistinguishable from random" if lo <= 0.5 <= hi
                       else ("PREDICTIVE" if a > 0.5 else "ANTI-predictive"))
            print(f"  -> {verdict}")

    vetoes = [r for r in usable if r["veto"]]
    if vetoes:
        app = sum(1 for r in vetoes if r["label"] == "Approved")
        print(f"\n--- the required veto fired {len(vetoes)} time(s) ---")
        print(f"  killed {app} Approved, caught {len(vetoes) - app} Rejected")
        for r in vetoes[:6]:
            print(f"    [{r['label']:8s}] {r['name'][:34]}")

    if args.min_rows and n < args.min_rows:
        print(f"\n  WARNING: n={n} is below --min-rows {args.min_rows}. "
              f"Treat everything above as directional only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
