"""
Score the AI metadata screen (LAYER 1) against the reviewer's own verdicts.

The question is narrow and the asymmetry is the whole point. Layer 1's job is
RECALL: it produces a broad list, and a second pass reads the transcript. So:

  - RECALL on Approved is the number that decides it. Every Approved channel the
    screen rejects is a real prospect lost, and nothing downstream recovers it.
    The bar is near-perfect, not "good".
  - Drop rate on Rejected is the benefit. Whatever it removes is reviewer
    attention saved and a Layer 2 request not spent.

This exists because being well-motivated is not evidence. The repo has caught
three inverted relevance criteria, and the closest analogue to this screen — an
AI reading channel metadata for niche fit — measured 27% approved against a 38%
base rate. An AI screen may well beat the keyword version it replaces, for the
reason main.off_target_reason gives; that reason is a hypothesis until measured.

Cost: ~3 YouTube quota units per channel (free 10k/day allowance) plus one Gemini
free-tier TEXT request per channel. Zero vendor credits. Everything is cached, so
a re-run costs nothing.

    python measure_metadata_screen.py --limit 80     # balanced sample
    python measure_metadata_screen.py --offline      # score what is cached
"""
import argparse
import json
import os
import random
import sys
import time
from collections import Counter

import config
import gemini_verify as gv
import niches
import video_topics as vt
from airtable_client import get_records

CACHE_PATH = os.getenv("METADATA_SCREEN_CACHE", "metadata_screen_cache.json")

# The TARGET given to the screen. Written from what the labels actually say the
# reviewer buys, not from the niche's name: the 2026-08 backtest found an
# equipment-focus score INVERTED against the verdict, and section 12 measured
# AV-specialist vocabulary catching only rejects. So the target is the AUDIENCE.
NICHE_BRIEFS = {
    "Home Theater": (
        "Creators whose audience would buy home-entertainment FURNITURE and "
        "room fittings — media rooms, man caves, basement builds, seating, TV "
        "walls, room tours, renovations, and the everyday life of a household "
        "that spends time in its living room. NOT specialist hi-fi or AV gear "
        "reviewing, which this reviewer consistently turns down."
    ),
    "Lifestyle Sofa": (
        "Creators whose audience would buy living-room furniture — home tours, "
        "interiors, decor, homemaking, family life at home, renovations and "
        "room makeovers."
    ),
}


def load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict) -> None:
    tmp = f"{CACHE_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    os.replace(tmp, CACHE_PATH)


def labelled_rows():
    rows = []
    for niche, table in (("Home Theater", config.AIRTABLE_TABLE_HOME_THEATER),
                         ("Lifestyle Sofa", config.AIRTABLE_TABLE_LIFESTYLE_SOFA)):
        if not table:
            continue
        for rec in get_records(table, fields=["Channel ID", "Channel Name", "Status"]):
            f = rec.get("fields", {})
            if f.get("Status") in ("Approved", "Rejected") and f.get("Channel ID"):
                rows.append({"niche": niche, "channel_id": f["Channel ID"],
                             "name": f.get("Channel Name", "?"), "label": f["Status"]})
    return rows


def fetch_metadata(channel_id: str) -> dict | None:
    from enrichment import get_channel_stats, get_recent_video_performance

    stats = get_channel_stats(channel_id)
    if not stats:
        return None
    perf = get_recent_video_performance(channel_id, stats.get("uploads_playlist_id"))
    if perf is None:
        return None
    return {
        "channel_title": stats.get("channel_title", ""),
        "bio": stats.get("description", ""),
        "video_titles": (perf.get("video_titles") or [])[:40],
        "video_descriptions": (perf.get("video_descriptions") or [])[:12],
        "tags": (perf.get("video_tags") or [])[:60],
        "categories": [vt.category_name(c)
                       for c in (perf.get("video_category_ids") or [])[:8]],
    }


# The free tier allows 15 requests/minute per model. The first run of this script
# lost 25 of 60 screens to 429s before that was known, so pace deliberately
# rather than relying on the retry.
FREE_TIER_RPM = int(os.getenv("SCREEN_RPM", 12))
_LAST_CALL = [0.0]


def _pace():
    gap = 60.0 / max(1, FREE_TIER_RPM)
    wait = gap - (time.monotonic() - _LAST_CALL[0])
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL[0] = time.monotonic()


def screen(meta: dict, niche: str, examples=None) -> dict | None:
    """One AI Layer 1 verdict, or None if the request failed."""
    body = gv.build_metadata_screen_request(
        niche, NICHE_BRIEFS.get(niche, ""),
        meta.get("channel_title", ""), meta.get("bio", ""),
        meta.get("video_titles"), meta.get("video_descriptions"),
        meta.get("tags"), meta.get("categories"),
        examples=examples,
    )
    _pace()
    v = gv.call(body, verdict_key="plausible", require_criteria=False)
    if not v.ok:
        return None
    return v.payload


def keyword_layer1_would_drop(meta: dict) -> bool:
    """The SHIPPING Layer 1, for comparison on the identical channels."""
    vocab = {**niches.EXCLUDED_TOPIC_TERMS, **niches.OFF_TARGET_TERMS}
    ev = vt.topic_evidence(meta.get("tags"), vocab)
    cat, _ = vt.dominant_topic(ev, config.VIDEO_TOPIC_MIN_SHARE)
    return bool(cat and cat in config.VIDEO_TOPIC_CATEGORIES)


def report(rows) -> None:
    app = [r for r in rows if r["label"] == "Approved"]
    rej = [r for r in rows if r["label"] == "Rejected"]
    print(f"\nscored {len(rows)} channels: {len(app)} Approved / {len(rej)} Rejected")
    if not rows:
        return

    for name, dropped in (("AI metadata screen (plausible=false)",
                           lambda r: r["ai"].get("plausible") is False),
                          ("keyword Layer 1 (shipping)",
                           lambda r: r["kw"])):
        lost = [r for r in app if dropped(r)]
        caught = [r for r in rej if dropped(r)]
        recall = 100 * (len(app) - len(lost)) / len(app) if app else 0.0
        print(f"\n=== {name} ===")
        print(f"  RECALL on Approved : {recall:5.1f}%   "
              f"({len(app) - len(lost)} of {len(app)} kept, {len(lost)} LOST)")
        print(f"  caught of Rejected : {len(caught)} of {len(rej)}"
              f"  ({100 * len(caught) / len(rej):.0f}%)" if rej else "")
        print(f"  net                : {len(caught) - len(lost):+d}"
              f"  (rejected caught minus approved lost)")
        for r in lost[:8]:
            why = r["ai"].get("reason", "")[:90] if name.startswith("AI") else "tags"
            print(f"    LOST  {r['niche'][:2]}  {r['name'][:30]:30s} {why}")

    # Confidence calibration on the AI screen: is a wrong answer at least
    # low-confidence? If not, confidence cannot be used as a safety valve.
    wrong = [r for r in app if r["ai"].get("plausible") is False]
    right = [r for r in app if r["ai"].get("plausible") is True]
    if wrong or right:
        m = lambda xs: (sum(xs) / len(xs)) if xs else 0.0
        print(f"\n  AI confidence when it KEPT an Approved  : "
              f"{m([r['ai'].get('confidence', 0) for r in right]):.2f}")
        print(f"  AI confidence when it LOST an Approved  : "
              f"{m([r['ai'].get('confidence', 0) for r in wrong]):.2f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=80,
                    help="balanced sample size (0 = every labelled channel)")
    ap.add_argument("--offline", action="store_true",
                    help="score only what is cached; make no requests")
    ap.add_argument("--few-shot", type=int, default=0,
                    help="show the model N approved and N rejected EXAMPLES per "
                         "niche, drawn from labelled channels held out of scoring. "
                         "The plain screen cannot know this reviewer approves "
                         "motorcycle and football channels for a furniture niche; "
                         "this tests whether being shown can teach it.")
    args = ap.parse_args(argv)

    cache = load_cache()
    rows = labelled_rows()
    if not rows:
        print("No labelled rows found.")
        return 1

    if args.limit:
        # Balanced, deterministic sample: recall on Approved is the number that
        # decides this, so the sample must not under-represent Approved.
        rnd = random.Random(20260825)
        app = [r for r in rows if r["label"] == "Approved"]
        rej = [r for r in rows if r["label"] == "Rejected"]
        rnd.shuffle(app); rnd.shuffle(rej)
        half = max(1, args.limit // 2)
        rows = app[:half] + rej[:half]

    # Few-shot examples are drawn from labelled channels and then EXCLUDED from
    # scoring, so nothing is graded on an example it was shown. Without that the
    # result is leakage, not a measurement.
    examples_by_niche, held_out = {}, set()
    if args.few_shot:
        rnd = random.Random(717)
        for niche in NICHE_BRIEFS:
            pool = [r for r in labelled_rows() if r["niche"] == niche]
            rnd.shuffle(pool)
            ex = {"approved": [], "rejected": []}
            for r in pool:
                bucket = "approved" if r["label"] == "Approved" else "rejected"
                if len(ex[bucket]) < args.few_shot and r["channel_id"] not in {
                        x["channel_id"] for x in rows}:
                    ex[bucket].append(r); held_out.add(r["channel_id"])
            # Fall back to taking from the scoring set if the pool is thin, and
            # remove those rows from scoring rather than grading on them.
            for r in pool:
                bucket = "approved" if r["label"] == "Approved" else "rejected"
                if len(ex[bucket]) < args.few_shot and r["channel_id"] not in held_out:
                    ex[bucket].append(r); held_out.add(r["channel_id"])
            examples_by_niche[niche] = {
                k: [x["name"] for x in v] for k, v in ex.items()}
        rows = [r for r in rows if r["channel_id"] not in held_out]
        print(f"few-shot: {args.few_shot} approved + {args.few_shot} rejected "
              f"example names per niche, held out of scoring "
              f"({len(held_out)} channels held out, {len(rows)} left to score)")

    scored, fetched, screened = [], 0, 0
    for i, row in enumerate(rows, 1):
        cid = row["channel_id"]
        entry = cache.get(cid) or {}
        meta = entry.get("meta")
        if meta is None:
            if args.offline:
                continue
            meta = fetch_metadata(cid)
            fetched += 1
            entry["meta"] = meta
            cache[cid] = entry
        if not meta:
            continue
        ai_key = f"ai_fs{args.few_shot}" if args.few_shot else "ai"
        ai = entry.get(ai_key)
        if ai is None:
            if args.offline:
                continue
            ai = screen(meta, row["niche"],
                        examples=examples_by_niche.get(row["niche"]))
            screened += 1
            entry[ai_key] = ai
            cache[cid] = entry
            if screened % 10 == 0:
                save_cache(cache)
                print(f"  ... {i}/{len(rows)} ({screened} screened)", file=sys.stderr)
        if not ai:
            continue
        scored.append({**row, "ai": ai, "kw": keyword_layer1_would_drop(meta)})

    if not args.offline:
        save_cache(cache)
    print(f"fetched {fetched} metadata, ran {screened} AI screens, "
          f"cache holds {len(cache)}")
    report(scored)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
