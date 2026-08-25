"""
How big is each niche's discovery pool, and which filter is costing the most?

Run this FIRST whenever a niche stops producing rows. It answers the question the
drop-reason counts cannot: whether the gates are too strict, or whether the niche
has simply run out of creators to look at.

Each probe is limit=1 and costs 0.01 credits, so a full ablation is under 0.1.
Nothing is enriched, nothing is written.

Measured 2026-08-21, and it is why this script exists: the Home Theater pool at a
10,000-subscriber floor was **208 creators in total**. At the measured 1 row per
100-150 creators the entire addressable universe was worth 1-2 rows, and ~64 of
the 208 were already tracked or rejected. Home Theater had not become too strict;
it had run out of pool. Lifestyle's pool was 1,498, which is exactly why it kept
producing rows.

Read the output as pool HEADROOM, not as a target: widening a filter that pulls in
off-niche creators costs 0.01 credits each on every run thereafter. The learnings
warn specifically against picking the wording with the biggest total.
"""
import copy, logging, sys
logging.basicConfig(level=logging.ERROR)
from channel_vetting.discovery import niches
from channel_vetting.discovery.influencers_club import InfluencerDiscovery

d = InfluencerDiscovery.from_config(max_credits=1.0)
if not d.enabled:
    sys.exit("discovery not configured")

def total(filters):
    # Goes through InfluencerDiscovery.probe, NOT _post. probe() is the one that
    # checks can_afford and writes record_spend; _post is only the HTTP call, so
    # calling it directly spent real credits the ledger never saw. See probe()'s
    # docstring for how much that was costing.
    _, t = d.probe(filters, limit=1, source_label="pool ablation probe")
    return t

# ---------------------------------------------------------------------------
# NET addressable pool.
#
# Everything above measures a GROSS pool: total() sends `filters` only, so the
# vendor answers "creators matching this query" and never "creators we can
# still buy". Those are very different numbers once a niche has been running.
# Home Theater's gross pool is ~334 while its reject cache alone holds 262
# handles, so the gross figure overstates the buyable pool by roughly 4x, and a
# plan sized against it is sized against nothing.
#
# Run with --net. Costs one extra probe per niche (0.01 credits each).
#
# CAVEAT worth reading before trusting the output: this assumes the vendor
# applies `exclude_handles` BEFORE computing `total`. If gross and net come back
# identical, that assumption is wrong and the number means nothing — the vendor
# is filtering the page but not the count. The script says so rather than
# quietly reporting a bad figure.
# ---------------------------------------------------------------------------
def net_pool():
    from channel_vetting import pipeline as _main
    from channel_vetting.discovery import rejected_handles as _rejected

    for niche in ("Home Theater", "Lifestyle Sofa"):
        base = niches.NICHES[niche]["discovery_filters"]
        gross = total(base)
        rejected = _rejected.for_niche(niche)
        # The exclusion the real run sends, minus the parts that need live
        # Airtable/blocklist reads — rejects dominate it for a mature niche.
        f = copy.deepcopy(base)
        f["exclude_handles"] = sorted(rejected)[:10000]
        net = total(f)
        print(f"\n{niche}")
        print(f"  gross pool                 {gross if gross is not None else 'failed':>8}")
        print(f"  reject cache               {len(rejected):>8}")
        print(f"  net (gross minus rejects)  {net if net is not None else 'failed':>8}")
        if gross and net == gross:
            print("  !! net == gross: the vendor is NOT applying exclude_handles to `total`.")
            print("     This number is meaningless; measure net by paging instead.")
        elif gross and net is not None:
            print(f"  -> {net/gross:.0%} of the gross pool is still buyable")


if "--net" in sys.argv:
    net_pool()
    raise SystemExit(0)


for niche in ("Home Theater", "Lifestyle Sofa"):
    base = niches.NICHES[niche]["discovery_filters"]
    b = total(base)
    print(f"\n{niche}   baseline total = {b:,}")
    print(f"  {'ablation':38} {'total':>7}   {'change':>9}")
    variants = [
        ("subscriber floor 10k -> 2.5k",  lambda f: f.update({"number_of_subscribers": {"min": 2500}})),
        ("drop the negation keyword list", lambda f: f.pop("keywords_not_in_description", None)),
        ("drop the gender filter",         lambda f: f.pop("gender", None)),
        ("drop the location filter",       lambda f: f.pop("location", None)),
        ("drop profile_language",          lambda f: f.pop("profile_language", None)),
        ("drop ai_search (query text)",    lambda f: f.pop("ai_search", None)),
    ]
    for label, mutate in variants:
        f = copy.deepcopy(base)
        mutate(f)
        t = total(f)
        if t is None:
            print(f"  {label:38} {'failed':>7}")
            continue
        pct = f"{(t/b - 1) * 100:+.0f}%" if b else "n/a"
        print(f"  {label:38} {t:>7,}   {pct:>9}")
    # the two most promising, together
    f = copy.deepcopy(base)
    f["number_of_subscribers"] = {"min": 2500}
    f.pop("gender", None)
    t = total(f)
    if t is not None:
        print(f"  {'subs 2.5k + no gender (combined)':38} {t:>7,}   {(t/b-1)*100:+.0f}%")
