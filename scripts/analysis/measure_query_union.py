"""
Does a PORTFOLIO of ai_search wordings reach more creators than one string?

This answers the single open question the yield plan rests on. Home Theater's
pool measured 334 creators against ONE ai_search string, and with the credit
budget frozen and the gender filter kept (operator decisions, 2026-08-22) a
wider union is the only remaining lever for that niche.

Totals alone CANNOT answer it. Two wordings returning 200 each are worth 400
creators if disjoint and 200 if identical, and `total` looks the same either
way. So this fetches actual HANDLES per variant and measures the union.

Cost. One page of 50 costs 0.5 credits, so a variant probed at limit=50 costs
0.5 and a 5-variant sweep costs ~2.5. That is real money against a 200/month
ledger, which is why --limit defaults to 20 (0.2/variant, ~1.0 for a sweep) and
why nothing here is wired into a run. Pass --dry-run to print the plan and cost
without issuing a single request.

Read the output as TWO numbers, and the second one is the one that matters:

  union size      - how many distinct creators the portfolio reaches
  marginal gain   - how many NEW creators each variant adds over its
                    predecessors. A variant adding <15% new is redundant
                    wording, not a wider net; drop it rather than pay 0.01 per
                    creator to rediscover the same people every run.

PRECISION IS NOT MEASURED HERE and this script must not be read as approving a
variant. discovery/niches.py records the rule the hard way: the biggest pool is how
"gaming setup" got in, carried 370 of 588 creators, and made 45% of that
niche's rows gaming channels. Judge a wording on WHO it returns. The --show
flag prints the top handles per variant precisely so that read is possible.
"""
import argparse, copy, logging, sys
logging.basicConfig(level=logging.ERROR)
from channel_vetting.discovery import niches
from channel_vetting.discovery.influencers_club import InfluencerDiscovery

# Candidate wordings per niche. Each must stay UNDER 150 CHARACTERS: the vendor
# documents 3-150 and a 180-char probe measured WORSE than a 122-char one,
# which reads like silent truncation. See discovery/niches.py for that measurement.
VARIANTS = {
    "Home Theater": [
        # v0 is the wording live in discovery/niches.py today — the baseline every other
        # variant's marginal gain is measured against. Do not reorder.
        None,
        "home cinema room, projector screen setup, surround sound install, "
        "AV receiver and speaker placement",
        "man cave build, basement hangout room, garage lounge, "
        "home bar and entertainment space",
        "hi-fi listening room, stereo speakers, turntable and vinyl setup, "
        "audiophile gear",
        "smart home living room, TV mounting and cable management, "
        "media console and furniture",
    ],
    "Lifestyle Sofa": [
        None,
        "living room makeover, sofa and sectional styling, "
        "coffee table decor, rug layering",
        "small apartment living, rental friendly decor, "
        "space saving furniture ideas",
        "family home organisation, playroom and nursery setup, "
        "cosy family living spaces",
    ],
}


def fetch_handles(d, filters, limit):
    """Distinct handles for one filter set, or None if the request failed."""
    # probe() rather than _post: it is the path that checks can_afford and
    # records the spend. A variant sweep at limit=20 costs ~2 credits, which is
    # far too much to leave off the ledger.
    accounts, total = d.probe(filters, limit=limit, source_label="query union probe")
    if accounts is None:
        return None, None
    handles = []
    for a in accounts:
        if not isinstance(a, dict):
            continue
        h = a.get("handle") or a.get("username") or a.get("profile_handle")
        if h:
            handles.append(str(h).lstrip("@").lower())
    return handles, (total if isinstance(total, int) else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=20,
                    help="results fetched per variant (default 20 = 0.2 credits each)")
    ap.add_argument("--niche", action="append",
                    help="restrict to one niche; repeatable")
    ap.add_argument("--show", type=int, default=0,
                    help="print the top N handles per variant, for the precision read")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and projected cost, issue no requests")
    args = ap.parse_args()

    targets = args.niche or list(VARIANTS)
    planned = sum(len(VARIANTS[n]) for n in targets if n in VARIANTS)
    projected = planned * args.limit * 0.01
    print(f"plan: {planned} variant probe(s) at limit={args.limit} "
          f"-> up to {projected:.2f} credits\n")
    if args.dry_run:
        for n in targets:
            for i, v in enumerate(VARIANTS.get(n, [])):
                print(f"  {n:16} v{i}  {'<live wording>' if v is None else v[:70]}")
        return

    d = InfluencerDiscovery.from_config(max_credits=projected + 0.5)
    if not d.enabled:
        sys.exit("discovery not configured (INFLUENCERS_API_KEY missing?)")

    for niche in targets:
        if niche not in VARIANTS:
            print(f"!! unknown niche {niche!r}, skipping")
            continue
        base = niches.NICHES[niche]["discovery_filters"]
        print(f"\n{niche}")
        print(f"  {'variant':8} {'vendor total':>12} {'fetched':>8} "
              f"{'new':>6} {'union':>7}  wording")
        union: set[str] = set()
        for i, wording in enumerate(VARIANTS[niche]):
            f = copy.deepcopy(base)
            if wording is not None:
                f["ai_search"] = wording
            handles, total = fetch_handles(d, f, args.limit)
            if handles is None:
                print(f"  v{i:<7} {'FAILED':>12}")
                continue
            fresh = set(handles) - union
            union |= set(handles)
            label = "<live>" if wording is None else wording[:52]
            print(f"  v{i:<7} {total:>12,} {len(handles):>8} "
                  f"{len(fresh):>6} {len(union):>7}  {label}")
            if args.show:
                for h in handles[:args.show]:
                    mark = "+" if h in fresh else " "
                    print(f"           {mark} @{h}")
        if union:
            base_only, _ = fetch_handles(d, copy.deepcopy(base), args.limit)
            n_base = len(set(base_only or []))
            gain = (len(union) / n_base - 1) * 100 if n_base else 0
            print(f"\n  union of {len(VARIANTS[niche])} variant(s): {len(union)} distinct "
                  f"creators vs {n_base} for the live wording alone ({gain:+.0f}%)")
            print("  NOTE: sampled at limit per variant, so this is the OVERLAP RATE, "
                  "not the full pool. Extrapolate against the vendor totals above.")
    print(f"\nspent: {d.credits_spent:g} credits")


if __name__ == "__main__":
    main()
