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
import niches
from influencer_discovery import InfluencerDiscovery

d = InfluencerDiscovery.from_config(max_credits=1.0)
if not d.enabled:
    sys.exit("discovery not configured")

def total(filters):
    r = d._post({"platform": "youtube", "paging": {"limit": 1, "page": 1},
                 "sort": {"sort_by": "relevancy", "sort_order": "desc"},
                 "filters": filters})
    if r is None:
        return None
    b = r.json().get("total")
    return b if isinstance(b, int) else 0

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
