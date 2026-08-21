"""Reconstruct the backtest from the CACHED verdicts — no new requests."""
import json
from collections import Counter

import config
from airtable_client import get_records

cache = json.load(open("gemini_cache.json"))
verdicts = {}
for key, entry in cache.items():
    parts = key.split("|")
    if parts[0] != "text":
        continue
    p = entry.get("payload") or {}
    if "relevance" in p:
        verdicts[parts[2]] = p

labels = {}
for niche, table in (("Home Theater", config.AIRTABLE_TABLE_HOME_THEATER),
                     ("Lifestyle Sofa", config.AIRTABLE_TABLE_LIFESTYLE_SOFA)):
    for rec in get_records(table, fields=["Channel ID", "Channel Name", "Status"]):
        f = rec.get("fields", {})
        if f.get("Status") in ("Approved", "Rejected") and f.get("Channel ID"):
            labels[f["Channel ID"]] = (niche, f.get("Channel Name", "?"), f["Status"])

rows = [(*labels[cid], v) for cid, v in verdicts.items() if cid in labels]
print(f"joined: {len(rows)} of {len(verdicts)} cached verdicts matched a labelled row\n")

def stats(subset, title):
    if not subset:
        return
    app = [v["relevance"] for _, _, s, v in subset if s == "Approved"]
    rej = [v["relevance"] for _, _, s, v in subset if s == "Rejected"]
    med = lambda a: sorted(a)[len(a) // 2] if a else None
    print(f"--- {title}  (n={len(subset)}: {len(app)} Approved / {len(rej)} Rejected) ---")
    print(f"  Approved relevance: median {med(app)}  mean {sum(app)/len(app):.0f}  range {min(app)}-{max(app)}" if app else "")
    print(f"  Rejected relevance: median {med(rej)}  mean {sum(rej)/len(rej):.0f}  range {min(rej)}-{max(rej)}" if rej else "")
    # 2x2 on the model's own on_niche boolean
    tab = Counter()
    for _, _, s, v in subset:
        tab[(bool(v.get("on_niche")), s)] += 1
    print(f"\n                     reviewer Approved   reviewer Rejected")
    print(f"  model on_niche=T          {tab[(True,'Approved')]:3}                 {tab[(True,'Rejected')]:3}")
    print(f"  model on_niche=F          {tab[(False,'Approved')]:3}                 {tab[(False,'Rejected')]:3}")
    on_t = tab[(True,'Approved')] + tab[(True,'Rejected')]
    on_f = tab[(False,'Approved')] + tab[(False,'Rejected')]
    base = len([1 for _,_,s,_ in subset if s=='Approved']) / len(subset)
    if on_t:
        print(f"\n  P(Approved | model says ON-niche)  = {tab[(True,'Approved')]/on_t:.0%}")
    if on_f:
        print(f"  P(Approved | model says OFF-niche) = {tab[(False,'Approved')]/on_f:.0%}")
    print(f"  base rate P(Approved)              = {base:.0%}")
    print()

stats(rows, "ALL NICHES")
for niche in ("Home Theater", "Lifestyle Sofa"):
    stats([r for r in rows if r[0] == niche], niche)

print("--- the 8 highest-relevance channels and what the reviewer said ---")
for n, name, s, v in sorted(rows, key=lambda r: -r[3]["relevance"])[:8]:
    print(f"  {v['relevance']:3}  {s:8}  {name[:40]:40} ({n})")
print("\n--- the 8 lowest-relevance channels ---")
for n, name, s, v in sorted(rows, key=lambda r: r[3]["relevance"])[:8]:
    print(f"  {v['relevance']:3}  {s:8}  {name[:40]:40} ({n})")
