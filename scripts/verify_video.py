"""
One-shot Gemini verification probe. No discovery, no credits, no Airtable.

    python scripts/verify_video.py https://www.youtube.com/watch?v=VIDEO_ID [--duration 1800]
    python scripts/verify_video.py --niche "Home Theater" <url>

WHY THIS EXISTS. `pipeline.py --test` runs the first niche only and needs a candidate
to survive ~14 upstream gates, so it can legitimately issue ZERO Gemini requests
while still spending YouTube quota and influencers.club credits — which makes
time-to-first-verdict unbounded and dependent on discovery luck. This gives a
verdict in ~10 seconds against a URL you choose, and it exercises the PRODUCTION
request path in gemini_verify rather than a copy, so what it proves is what the
pipeline will do.

It answers three questions the plan could not answer by reading documentation:

  1. Is the request SHAPE right? `fileData`/`videoMetadata` placement,
     `mediaResolution` in generationConfig, `responseSchema` — all documented but
     unproven until a 200 comes back.
  2. Which model actually served it? `modelVersion` is the only server-side
     statement of that, and it is the operator's routine proof of free-tier use.
  3. **Is the free tier's 8h/day YouTube ceiling metered on the CLIP or on the
     SOURCE video?** Google's docs are silent. `promptTokenCount` settles it: at
     MEDIA_RESOLUTION_LOW a 25s clip is ~2.5-3k tokens, so a long source video
     reporting that means the clip is what gets processed. Six figures means the
     whole upload is being ingested and the video tier's headroom is far tighter.
"""
import argparse
import json
import logging
import sys

from channel_vetting import config
from channel_vetting.verification import gemini as gv

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# Used when the niche has no video_criteria yet. Deliberately the real questions
# from the plan, not placeholders, so the probe exercises a realistic prompt.
FALLBACK_CRITERIA = [
    {"name": "on-camera creator",
     "test": "Is a real person presenting to camera or narrating their own "
             "footage, rather than reposted manufacturer or stock material?"},
    {"name": "subject is the content",
     "test": "Is the video's own subject the equipment or space being discussed, "
             "rather than it appearing incidentally in the background?"},
]


def video_id_from(url: str) -> str:
    if "v=" in url:
        return url.split("v=")[1].split("&")[0]
    return url.rstrip("/").rsplit("/", 1)[-1]


def criteria_for(niche_name):
    if not niche_name:
        return FALLBACK_CRITERIA, "built-in fallback"
    try:
        from channel_vetting.discovery import niches
        cfg = niches.NICHES.get(niche_name) or {}
        crit = cfg.get("video_criteria")
        if crit:
            return crit, f"niches.NICHES[{niche_name!r}]['video_criteria']"
    except Exception as e:  # noqa: BLE001 - probe, report and fall back
        print(f"  (could not read niches: {e})")
    return FALLBACK_CRITERIA, f"built-in fallback (no video_criteria on {niche_name!r})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--niche", default=None)
    ap.add_argument("--duration", type=int, default=None,
                    help="Source length in seconds. Drives the clip offsets; "
                         "the pipeline reads it from videos.list.")
    args = ap.parse_args()

    vid = video_id_from(args.url)
    crit, crit_src = criteria_for(args.niche)
    body = gv.build_video_request(vid, args.duration, crit)
    vm = body["contents"][0]["parts"][0]["videoMetadata"]

    print("=" * 72)
    print("CONFIG")
    print(f"  key set          : {bool(config.GEMINI_API_KEY)}")
    print(f"  GEMINI_ENABLED   : {config.GEMINI_ENABLED}  (irrelevant here — the "
          f"probe is explicit)")
    print(f"  GEMINI_FREE_ONLY : {config.GEMINI_FREE_ONLY}")
    print(f"  model requested  : {config.GEMINI_MODEL}")
    print(f"  allowlisted      : {gv.model_is_allowed(config.GEMINI_MODEL)}")
    print(f"  allowlist        : {', '.join(sorted(config.GEMINI_FREE_TIER_MODELS))}")
    print(f"  criteria from    : {crit_src}")
    print()
    print("REQUEST")
    print(f"  POST {config.GEMINI_BASE_URL}/models/{config.GEMINI_MODEL}:generateContent")
    print(f"  video            : {vid}")
    print(f"  source duration  : {args.duration if args.duration else 'unknown'}")
    print(f"  clip window      : {vm['startOffset']} -> {vm['endOffset']}  (fps={vm['fps']})")
    for k in ("tools", "toolConfig", "cachedContent"):
        assert k not in body, f"billable feature {k} present in request"
    assert "temperature" not in body["generationConfig"], "temperature is deprecated"
    print("  billable features: none (no tools / toolConfig / cachedContent)")
    print()
    print(json.dumps(body, indent=2)[:1400])
    print()
    print("CALLING…")
    v = gv.call(body)
    print()
    print("RESULT")
    print(f"  reason_code      : {v.reason_code}")
    print(f"  modelVersion     : {v.model_version or '(none)'}")
    print(f"  served allowlisted: {gv._served_model_allowed(v.model_version)}")
    print(f"  promptTokenCount : {v.tokens}")
    if v.tokens:
        clip_s = int(vm["endOffset"].rstrip("s")) - int(vm["startOffset"].rstrip("s"))
        print(f"  tokens/second    : {v.tokens / max(clip_s, 1):.0f}   "
              f"(~100/s at LOW means the CLIP was processed, not the whole video)")
    if v.payload:
        print()
        print("  VERDICT")
        print("  " + json.dumps(v.payload, indent=2).replace("\n", "\n  "))
    print("=" * 72)
    return 0 if v.ok else 1


if __name__ == "__main__":
    sys.exit(main())
