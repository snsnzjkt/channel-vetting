"""
The spoken text of a YouTube video, fetched free and cached.

## Why this module exists, and the mistake it corrects

An earlier pass in this repo concluded transcripts were UNOBTAINABLE and built
around that: it probed the caption `baseUrl` from the watch page, `&fmt=json3`,
`&fmt=srv3` and bare `/api/timedtext`, with and without a Referer, got HTTP 200
with an EMPTY BODY from every one, and wrote the conclusion down as measured
fact. That conclusion was wrong. The probes were right and the inference was not:
those raw endpoints really are closed, and `youtube_transcript_api` reaches the
same captions through a different client context. Measured 2026-08-25 on the same
video IDs that returned zero bytes: 1,838 and 4,155 characters of real text.

The lesson is written here rather than in a commit message because it is the kind
of thing that gets re-derived: "the obvious HTTP route is blocked" is not the
same finding as "the data is unavailable", and the gap between them is a library
somebody wrote precisely to close it.

`captions.download` on the YouTube Data API is still genuinely unusable — it
requires OAuth as the CHANNEL OWNER, so it cannot read a third party's captions
at any price. That part was correct.

## What this costs

Nothing. No API key, no YouTube Data API quota, no vendor credits, no Gemini
request. ~1 second per video, and a transcript is a few hundred to a couple of
thousand tokens for a WHOLE video — against ~5,940 tokens for a 90-second video
window at MEDIA_RESOLUTION_LOW. It is cheaper and covers more.

## What it does NOT provide

Not every video has captions. Of the three videos used to verify this, one had
them disabled outright. That is why every failure path here returns None rather
than raising: absent data never disqualifies, so a candidate with no transcript
gets no verdict from this layer, exactly as it would if the layer did not exist.

The one real operational risk is IP blocking — YouTube rate-limits this route,
and `IpBlocked` is a documented exception. It is mitigated by volume rather than
by cleverness: the only caller runs on candidates a free metadata gate already
flagged, which is ~2% of them, so a run makes a handful of fetches. Results are
cached on disk so a re-run of the same candidate makes none.
"""
import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

TRANSCRIPT_CACHE_FILE = os.getenv("TRANSCRIPT_CACHE_FILE", "transcript_cache.json")

# A cap on what one transcript contributes downstream. A three-hour stream would
# otherwise dominate a prompt and the token bill. Cut at a word boundary so the
# text never ends mid-word, which reads as corruption to whatever consumes it.
MAX_TRANSCRIPT_CHARS = int(os.getenv("MAX_TRANSCRIPT_CHARS", 12_000))

# A transcript this short is not evidence of anything — a music video's
# "[music]" track, or a few stray auto-caption fragments.
MIN_TRANSCRIPT_CHARS = int(os.getenv("MIN_TRANSCRIPT_CHARS", 200))

_LOCK = threading.Lock()
_CACHE = None


def _load_cache() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        with open(TRANSCRIPT_CACHE_FILE, encoding="utf-8") as fh:
            loaded = json.load(fh)
        _CACHE = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        _CACHE = {}
    return _CACHE


def flush_cache() -> None:
    """
    Persist the cache. Atomic via os.replace, like every other state file here.

    A miss is a wasted second, never a wrong answer, so a failed write is logged
    and swallowed rather than stopping a run.
    """
    cache = _load_cache()
    try:
        tmp = f"{TRANSCRIPT_CACHE_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, sort_keys=True)
        os.replace(tmp, TRANSCRIPT_CACHE_FILE)
    except OSError as exc:
        logger.warning("Could not persist the transcript cache: %s", exc)


def _truncate(text: str) -> str:
    if len(text) <= MAX_TRANSCRIPT_CHARS:
        return text
    cut = text[:MAX_TRANSCRIPT_CHARS]
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else cut).rstrip() + " …"


def fetch(video_id: str, languages=("en",), api=None) -> str | None:
    """
    The spoken text of `video_id`, or None when there is none to be had.

    None — never an exception — for every failure: captions disabled, no track in
    the requested languages, an unusable video id, an age-restricted or
    unavailable video, an IP block, a network error, or a transcript too short to
    mean anything. Each is logged at the level that matches what an operator
    should do about it: an IP block is a WARNING because it affects every
    subsequent fetch, while captions being disabled is routine and is INFO.

    `api` is injectable so tests never touch the network. Left None in
    production, which is also why the import is inside the function: the
    dependency stays optional, and a missing library degrades this layer to
    "no transcript available" instead of breaking the import graph of main.py.

    Cached in memory and on disk, keyed by video id and language preference. A
    NEGATIVE result is cached too, as an empty string: a video whose captions are
    disabled will still be disabled next run, and re-asking costs a second and
    edges toward the rate limit for no gain.
    """
    vid = (video_id or "").strip()
    if not vid:
        return None

    key = f"{vid}|{','.join(languages)}"
    cache = _load_cache()
    with _LOCK:
        if key in cache:
            hit = cache[key]
            return hit or None

    if api is None:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError:
            logger.info(
                "youtube-transcript-api is not installed, so no transcript is "
                "available. This layer degrades to no verdict, which is the same "
                "as it not existing. Install it to enable transcript checks."
            )
            return None
        api = YouTubeTranscriptApi()

    try:
        fetched = api.fetch(vid, languages=languages)
    except Exception as exc:  # noqa: BLE001 — the library raises ~10 distinct types
        name = type(exc).__name__
        if name in ("IpBlocked", "RequestBlocked"):
            # The one failure worth shouting about: it is not about this video,
            # it affects every fetch after it, and the fix is operational.
            logger.warning(
                "YouTube is blocking transcript requests from this IP (%s). Every "
                "transcript fetch will fail until that clears; candidates simply "
                "get no transcript verdict, which never drops a row.", name,
            )
        else:
            logger.info("No transcript for %s (%s).", vid, name)
        with _LOCK:
            # Cache the negative EXCEPT for a block, which is about us and not
            # about the video — caching it would poison every video seen while
            # the block lasted.
            if name not in ("IpBlocked", "RequestBlocked"):
                cache[key] = ""
        return None

    try:
        text = " ".join(s.text for s in fetched if getattr(s, "text", "").strip())
    except TypeError:
        # Defensive: a library version returning plain dicts rather than objects.
        text = " ".join(str(s.get("text", "")) for s in fetched or [])

    text = " ".join(text.split())
    if len(text) < MIN_TRANSCRIPT_CHARS:
        logger.info("Transcript for %s is only %d chars — treating as none.",
                    vid, len(text))
        with _LOCK:
            cache[key] = ""
        return None

    text = _truncate(text)
    with _LOCK:
        cache[key] = text
    return text
