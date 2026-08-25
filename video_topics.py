"""
What a channel's videos are ABOUT, from topic data already paid for.

## Why this module exists

Every relevance signal in this pipeline reads one of two things: what a channel
is CALLED (`excluded_topic_reason`, `broadcast_tv_reason`, `location_drop_reason`
— channel title and About bio) or what it NAMES its videos
(`off_target_reason` — the last ~50 video titles). Neither reads what a video is
about. So the vocabularies are already right and the input is the gap:
`EXCLUDED_TOPIC_TERMS["firearms"]` contains "firearm", "handgun", "ammo", and
`OFF_TARGET_TERMS["toys_and_kids"]` contains "lego", "minifigure", "brickheadz"
— but a firearms channel titling videos "Range Day 47" and a Lego channel
titling one "New Build Complete!" match none of them.

`snippet.tags` is the creator's own topic labelling of each video and
`snippet.categoryId` is YouTube's. Both arrive FREE on the `videos.list`
response `get_recent_video_performance` already makes (that call is a flat 1
quota unit regardless of how many parts are requested, and `snippet` is already
requested for `defaultAudioLanguage`). Until 2026-08-24 both were discarded —
the same oversight that lost `video_titles` until they were wired into
`off_target_reason`.

## Why not a transcript

A transcript is the better signal and it is not obtainable:

  - `captions.download` on the YouTube Data API requires OAuth as the CHANNEL
    OWNER. There is no third-party route, at any price. This is a documented
    Google restriction, not a rate limit.
  - Every unauthenticated caption route answers **HTTP 200 with an empty body**
    (measured 2026-08-24 across `baseUrl`, `&fmt=json3`, `&fmt=srv3`, with and
    without a Referer). The track LISTING is still readable from the watch page,
    so it is possible to learn that English auto-captions exist and impossible
    to read them.
  - Local ASR (Whisper et al.) needs the audio, which means downloading it, and
    that is both against YouTube's terms and infeasible here on wall clock: the
    2026-08-24 Home Theater run already took 985s against a 900s Gemini brake,
    and ASR is minutes per video across ~90 candidates.

What the pipeline DOES already have is Gemini watching a 25-second clip with its
audio (`gemini_verify.build_video_request` sends the video URL, not a
transcript, and the model ingests both tracks). So "what is said" is reachable
through the criteria there, on a 25-second window, and this module covers the
whole sampled catalogue instead. They are complements.

## The standing rule this module obeys

Negative evidence only, and absent data never disqualifies. No tags means no
verdict — never a drop. Nothing here can admit a channel on its own; a positive
must-match relevance gate was built, measured and REJECTED twice in this repo
(see `main.off_target_reason` and `config.GEMINI_TEXT_TIER`). This reports
evidence; it does not decide.
"""
import re
from collections import Counter

# YouTube's assignable video categories. Only the ones a candidate here could
# plausibly carry are named; anything else reports as its bare ID rather than
# being silently dropped, so an unexpected category is visible in the record.
YOUTUBE_CATEGORY_NAMES = {
    "1": "Film & Animation", "2": "Autos & Vehicles", "10": "Music",
    "15": "Pets & Animals", "17": "Sports", "19": "Travel & Events",
    "20": "Gaming", "22": "People & Blogs", "23": "Comedy",
    "24": "Entertainment", "25": "News & Politics", "26": "Howto & Style",
    "27": "Education", "28": "Science & Technology", "29": "Nonprofits",
}


# Human-readable phrasing for each vocabulary key, for the topic-confirmation
# PROMPT. The keys are internal snake_case identifiers ("toys_and_kids"), and
# handing a model an identifier instead of a description is a needless handicap:
# it has to guess the intent of the name before it can answer the question.
# A key with no entry here falls back to its name with underscores spaced out,
# which is poor but never silently empty.
TOPIC_LABELS = {
    "firearms": "firearms — guns, shooting or ammunition",
    "toys_and_kids": "toys and children's play — dolls, action figures, "
                     "construction-brick sets, or toy unboxing",
    "asmr": "ASMR — whispered or trigger-sound relaxation content",
    "political": "party politics — elections, campaigning or political commentary",
    "gaming": "video gaming — playing, reviewing or commentating on video games",
    "phones_and_pcs": "consumer computing hardware — phones, laptops or PC builds",
    "generic_gadgets": "general consumer gadget reviews",
    "ai_and_crypto": "AI tools or cryptocurrency",
    "automotive": "cars and vehicles",
    "sports_commentary": "sports commentary, punditry or league coverage",
    "story_recap": "narrated recaps of films, series or comics",
    "av_specialist": "specialist hi-fi and audio equipment — speakers, "
                     "amplifiers, turntables or headphones reviewed as gear",
    "travel_vlog": "travel vlogging",
    "property_showcase": "property listings or real-estate showcases",
    "sim_racing": "sim racing games",
    "forestry": "forestry, logging or sawmilling",
    "kids_craft": "children's craft activities",
    "movie_review_farm": "film reviews and reaction content",
    "broadcast_tv": "broadcast television or radio programming, or a media outlet's own channel",
    "music_perf": "music performance",
    "food_only": "cooking and food",
    "news_politics": "news and current affairs",
    "reaction_farm": "reaction content",
    "sports_league": "professional sports league coverage",
    "realestate_listing": "real-estate listings",
}


def topic_label(topic: str) -> str:
    """A phrase a model can act on, for an internal vocabulary key."""
    key = (topic or "").strip()
    if not key:
        return ""
    return TOPIC_LABELS.get(key, key.replace("_", " "))


def category_name(category_id) -> str:
    """A readable category, or the bare ID when YouTube adds one we don't know."""
    cid = str(category_id or "").strip()
    if not cid:
        return ""
    return YOUTUBE_CATEGORY_NAMES.get(cid, f"category {cid}")


def category_distribution(category_ids) -> list:
    """
    [(name, count)] over the sampled window, most common first.

    Reported rather than gated. A category is far too coarse to drop on — a
    genuine home-theatre creator and a gaming channel both sit in "Gaming" often
    enough that the measurement in `measure_video_topics.py` is the only thing
    that could justify using it, and it has not been run.
    """
    counts = Counter(category_name(c) for c in (category_ids or []) if c)
    return counts.most_common()


def _compile(vocabularies: dict) -> dict:
    """
    {category: pattern} over each vocabulary, matched on a word boundary.

    Word-boundary matching matters more here than on titles: a tag is a short
    standalone phrase, so substring matching would fire "iem" inside "item" and
    "dac" inside "dachshund". The existing title gates carry leading spaces on
    exactly those terms to work around it; a boundary does it properly.
    """
    compiled = {}
    for category, terms in (vocabularies or {}).items():
        cleaned = [t.strip() for t in (terms or []) if t and t.strip()]
        if not cleaned:
            continue
        # Longest-first so "bookshelf speaker" is preferred over "speaker" when
        # both would match — the reported term should be the specific one.
        cleaned.sort(key=len, reverse=True)
        compiled[category] = re.compile(
            r"\b(?:" + "|".join(re.escape(t) for t in cleaned) + r")\b",
            re.IGNORECASE,
        )
    return compiled


def topic_evidence(tags, vocabularies: dict) -> dict:
    """
    Which excluded topics the creator's own tags evidence, and how strongly.

        {"hits": {category: n_tags},
         "terms": {category: [matched term, ...]},
         "share": {category: n_tags / n_tags_total},
         "tags_seen": n_tags_total}

    `share` is the number that matters and it is deliberately computed over
    TAGS, not videos: a channel that tags one video "lego" among 400 tags is not
    a Lego channel, and one where 60 of 90 tags name Lego sets is. `hits` alone
    would treat those the same, which is the mistake `off_target_reason` avoids
    by scoring share over titles rather than counting matches.

    Empty in, empty out — never a verdict on no data.
    """
    seen = [t for t in (tags or []) if t and t.strip()]
    result = {"hits": {}, "terms": {}, "share": {}, "tags_seen": len(seen)}
    if not seen:
        return result
    for category, pattern in _compile(vocabularies).items():
        matched_terms, n = [], 0
        for tag in seen:
            found = pattern.search(tag)
            if found:
                n += 1
                matched_terms.append(found.group(0).lower())
        if n:
            result["hits"][category] = n
            # Deduped, order-stable, capped: this string reaches Airtable and a
            # 400-tag channel would otherwise write a paragraph of repeats.
            result["terms"][category] = list(dict.fromkeys(matched_terms))[:8]
            result["share"][category] = n / len(seen)
    return result


def dominant_topic(evidence: dict, min_share: float) -> tuple:
    """
    (category, share) for the strongest topic at or above `min_share`, else
    (None, 0.0).

    A threshold, not a ranking: the caller wants "is this channel dominated by
    an excluded topic", and a category that labels 2% of tags is noise whatever
    its rank. Ties break on the higher share, then alphabetically so the answer
    is stable across runs for the same input.
    """
    shares = (evidence or {}).get("share") or {}
    ranked = sorted(((s, c) for c, s in shares.items() if s >= min_share),
                    key=lambda pair: (-pair[0], pair[1]))
    if not ranked:
        return None, 0.0
    share, category = ranked[0]
    return category, share


def summarise(evidence: dict, category_ids=None) -> str:
    """
    One reviewer-readable line, or "" when there is nothing to say.

    Written for the Airtable cell, so it leads with the share (the decision
    number) and names the terms that produced it — the same shape as the
    Gemini notes field, for the same reason: a reviewer overruling a machine
    needs to see the evidence, not the conclusion.
    """
    if not evidence or not evidence.get("hits"):
        cats = category_distribution(category_ids)
        return f"no topic tags matched; categories {cats}" if cats else ""
    parts = []
    for category, share in sorted(evidence["share"].items(), key=lambda kv: -kv[1]):
        terms = ", ".join(evidence["terms"].get(category, []))
        parts.append(f"{category} {share:.0%} ({evidence['hits'][category]}"
                     f" of {evidence['tags_seen']} tags: {terms})")
    cats = category_distribution(category_ids)
    line = "; ".join(parts)
    return f"{line} | categories {cats}" if cats else line
