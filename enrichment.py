"""
Enriches discovered channels with real stats via channels.list,
playlistItems.list, and videos.list.

Every call here goes through the shared retrying YouTube session in
`http_client.py` (imported as HTTP), which carries the API key as a header
rather than a `key=` query parameter — see that module's docstring.

Nothing in this module raises on a failed lookup. A non-200, an empty
result, an unparseable statistic and an unreachable API all resolve to
None (or "" for the email scan) with a logged warning, because the only
caller — main.process_candidate() — handles a falsy return by skipping the
candidate and has no handler at all for an exception. An exception escaping
this module unwinds all the way through push_until_full() into run_niche()
and ends the run.
"""
import logging
import re
import time
from collections import Counter
from datetime import datetime, timezone

# Kept for `requests.RequestException` only — the actual calls go through the
# shared retrying session below, never through the module-level
# `requests.get()`.
import requests

from config import (
    YOUTUBE_API_BASE_URL,
    QUOTA_COST_CHANNELS_LIST,
    QUOTA_COST_PLAYLIST_ITEMS_LIST,
    QUOTA_COST_VIDEOS_LIST,
    API_SLEEP_SECONDS,
    EMAIL_DEEP_SCAN_PAGES,
    LONGFORM_SCAN_MAX_PAGES,
)
from http_client import YOUTUBE as HTTP, safe_body
from quota_tracker import record_spend

logger = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
HANDLE_PATTERN = re.compile(r"@([a-zA-Z0-9_.\-]+)")


def normalize_handle(raw: str) -> str:
    """
    Extract and normalize a YouTube @handle from either a full URL (e.g.
    "https://www.youtube.com/@Foo/videos") or a bare handle ("@Foo" or the
    raw value channels.list returns in snippet.customUrl). Returns "" if
    no @handle is present (e.g. a legacy /c/ or /user/ channel that never
    set one — those can't be matched this way).
    """
    if not raw:
        return ""
    match = HANDLE_PATTERN.search(raw)
    return match.group(1).lower() if match else ""

# A one-off email mention in a single video's description is weak evidence
# (could be a shoutout, a giveaway, someone else's contact, etc.). Seeing
# the same address repeated across several videos' descriptions is a much
# stronger signal it's the creator's actual standing contact email.
EMAIL_MIN_VIDEO_REPEATS = 3

# How many recent videos to pull descriptions from when hunting for a
# contact email. 50 is the API maximum for both playlistItems.list and a
# single videos.list call, and — crucially — costs exactly the same 2
# quota units as asking for 10 would. More description text is free, so
# there is no reason to ask for less.
EMAIL_SCAN_SAMPLE_SIZE = 50

# ...but the performance metrics stay on the most recent 10 videos. The
# Airtable column is literally named "Avg Views (last 10 videos)", and
# widening this window would shift avg views, engagement rate and upload
# cadence — and therefore Overall Score — for every newly processed
# channel, making them incomparable with the records already in the base.
# Widen the email scan, hold the scoring baseline still.
PERFORMANCE_SAMPLE_SIZE = 10

# A video's view count keeps climbing for a while after it's posted, so the
# per-video floor (main.MIN_VIEWS_PER_VIDEO) only judges videos public at
# least this long. Without it, a channel's freshest upload — still climbing
# toward the floor — would sink the whole channel via the window MINIMUM,
# dropping exactly the actively-uploading creators the recency/cadence gates
# want to keep. 14 days is well past the bulk of a typical video's view accrual.
PERFORMANCE_MATURITY_DAYS = 14


# Domains belonging to a third party that routinely shows up in creator
# descriptions — shared platforms, link-aggregators, payment/tip/coupon
# services, freelance marketplaces and no-code tools (sponsor plugs, tip
# jars, "logo by ..."). An address or website at one of these is that
# service's, never the creator's. Confirmed necessary in testing: a
# "Cash App tip jar" mention produced an "@cash.app" match via the
# repeated-email path.
THIRD_PARTY_DOMAINS = {
    "youtube.com", "youtu.be", "instagram.com", "tiktok.com", "twitter.com",
    "x.com", "facebook.com", "fb.com", "linktr.ee", "linktree.com",
    "beacons.ai", "beacons.page", "discord.gg", "discord.com", "patreon.com",
    "twitch.tv", "amzn.to", "amazon.com", "bit.ly", "goo.gl", "linkedin.com",
    "threads.net", "snapchat.com", "pinterest.com", "reddit.com",
    "google.com", "apple.com", "spotify.com",
    # Payment / tip-jar / coupon-referral services
    "cash.app", "venmo.com", "paypal.com", "paypal.me", "buymeacoffee.com",
    "ko-fi.com", "coupert.com", "honey.com",
    # URL shorteners — the shortener's own domain, not the (unknown)
    # destination, is what gets queried, so these can never be trusted.
    "tinyurl.com", "t.co", "ow.ly", "rebrand.ly", "is.gd", "tiny.cc", "cutt.ly",
    # Generic freelance-marketplace / form-builder / no-code tool platforms
    # creators routinely credit or link to (a logo designer's Fiverr gig, a
    # Tally.so submission form, a Canva design, etc.) — an address at one
    # of these is that PLATFORM's own business email, not the creator's,
    # even when the creator is using the tool themselves.
    "fiverr.com", "upwork.com", "freelancer.com", "99designs.com",
    "tally.so", "typeform.com", "forms.gle", "docs.google.com",
    "calendly.com", "canva.com", "elink.io",
}

# Free consumer mail providers. Unlike THIRD_PARTY_DOMAINS these are NOT
# someone else's domain — a creator's real contact address is very often
# a gmail one. Kept separate from EMAIL_DOMAIN_BLOCKLIST for that reason
# (see below) — still used for reporting in backfill_missing_emails.py.
FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "yahoo.com", "ymail.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "protonmail.com", "proton.me", "gmx.com", "gmx.de",
    "mail.com", "zoho.com", "yandex.com", "yandex.ru", "fastmail.com",
}

# What can never be the creator's own *email address* — third-party
# services ONLY. Freemail is deliberately absent: folding these two
# lists into one silently discarded every @gmail.com match, which is by
# far the most common kind of creator contact address (53% of the
# addresses this pipeline had collected at the time this was split).
EMAIL_DOMAIN_BLOCKLIST = THIRD_PARTY_DOMAINS


# The API exposes no "is this a Short" flag, so duration is the proxy.
# 60s is the classic Shorts cap. YouTube raised it to 3 minutes in late
# 2024, so a 60s cutoff under-detects newer Shorts channels — chosen
# deliberately: a channel misread as Shorts-only is DISCARDED before it
# ever reaches Airtable (main.pre_push_drop_reason), so a false positive
# costs a real prospect with no row left to review, while a false negative
# only costs one flagged row a human can dismiss.
SHORTS_MAX_SECONDS = 60

ISO8601_DURATION_PATTERN = re.compile(
    r"^P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def is_blocklisted_email_domain(domain: str) -> bool:
    return domain.lower() in EMAIL_DOMAIN_BLOCKLIST


def is_blocklisted_email(email: str) -> bool:
    """
    `is_blocklisted_email_domain` for a whole address.

    The "split the domain off, lowercase it, look it up" idiom had been
    written out at every call site that screens an address, which put the
    domain-extraction rule in several places while the list it guards lives
    in one. A future refinement (a trailing dot, a subdomain match) belongs
    here, next to the list, rather than in each caller.
    """
    return is_blocklisted_email_domain(email.rsplit("@", 1)[-1])


def parse_iso8601_duration(value: str | None) -> int | None:
    """
    Seconds for an ISO 8601 duration like videos.list returns ("PT12M35S"),
    or None when it's missing or unparseable.

    None means "unknown", never "zero" — see is_shorts_only().
    """
    if not value:
        return None
    match = ISO8601_DURATION_PATTERN.match(value.strip())
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    total = (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )
    # "P0D" and friends parse structurally but carry no duration; a
    # zero-second video is not evidence of anything.
    return total or None


def is_shorts_only(durations) -> bool:
    """
    True only when every sampled video is a Short, i.e. no evidence of any
    long-form upload.

    Returns False whenever ANY duration is unknown, and False for an empty
    sample. This asymmetry is deliberate: the caller discards a
    Shorts-only channel outright, so the burden of proof sits on excluding,
    not on including.
    """
    if not durations:
        return False
    parsed = [parse_iso8601_duration(d) for d in durations]
    if any(seconds is None for seconds in parsed):
        return False
    return all(seconds <= SHORTS_MAX_SECONDS for seconds in parsed)


def count_longform(durations) -> int:
    """
    How many of `durations` are confirmed NOT Shorts, i.e. longer than
    SHORTS_MAX_SECONDS.

    Counts only durations that actually parse. An unreadable duration is
    unknown, not long-form, so it never counts toward the total — the
    opposite lean from is_shorts_only(), and for the same reason. This number
    feeds a MINIMUM (main.MIN_LONGFORM_VIDEO_COUNT), so counting an unknown
    as long-form would let a channel clear the bar on missing data. Here the
    burden of proof sits on the channel to show a long-form catalogue.
    """
    return sum(
        1 for d in durations
        if (parse_iso8601_duration(d) or 0) > SHORTS_MAX_SECONDS
    )


def dominant_language(languages) -> str:
    """
    The most common non-empty language tag in a sample, or "" if none is set.

    Ties break toward the first-seen tag (Counter.most_common is insertion-
    stable for equal counts), so the newest upload wins a genuine tie.

    Reading the MOST COMMON rather than the first non-empty one matters now
    that the value is gated on: a single mislabelled upload (an `es` reaction
    video on an otherwise English channel, or vice versa) would otherwise
    decide the whole channel's fate. The tag is set per-video by the creator
    and is inconsistent in exactly this way.
    """
    counts = Counter(lang for lang in languages if lang)
    return counts.most_common(1)[0][0] if counts else ""


def extract_business_email(description: str) -> str:
    """
    Best-effort extraction of a plain-text contact email from a channel's
    public About description.

    Note: YouTube deliberately does NOT expose a channel's "business
    inquiries" email through the Data API — it's gated behind a
    CAPTCHA-protected reveal button on the About page specifically to
    block scraping, and we don't attempt to circumvent that. This only
    picks up an email if a creator has typed one directly into their
    (fully public, already-fetched) description text, e.g.
    "Business inquiries: name@studio.com". Returns "" if none found.
    """
    if not description:
        return ""
    for candidate in EMAIL_PATTERN.findall(description):
        if not is_blocklisted_email(candidate):
            return candidate
    return ""


def find_repeated_email(video_descriptions: list[str]) -> str:
    """
    Scan a set of video descriptions (same channel) for an email address
    that recurs across at least EMAIL_MIN_VIDEO_REPEATS distinct videos —
    a much stronger "this is really their contact email" signal than a
    single mention. Returns the most-repeated qualifying email, or "" if
    none clears the threshold.
    """
    video_counter = Counter()
    for description in video_descriptions:
        if not description:
            continue
        # dedupe within a single video's description so one video that
        # happens to print the same address twice doesn't inflate its count
        emails_in_video = {
            e for e in set(EMAIL_PATTERN.findall(description))
            if not is_blocklisted_email(e)
        }
        video_counter.update(emails_in_video)

    if not video_counter:
        return ""

    email, count = video_counter.most_common(1)[0]
    return email if count >= EMAIL_MIN_VIDEO_REPEATS else ""


# Average days per month, for turning a channel's age into the "months"
# unit the briefs are written in. Approximate on purpose — nothing here
# depends on calendar-exact month boundaries.
DAYS_PER_MONTH = 30.44


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    """
    An ISO 8601 timestamp (YouTube's trailing-'Z' form) as a tz-aware
    datetime, or None when it is missing or unparseable.

    The single home of the tolerant-parse rule, shared by
    channel_age_months() and days_since_last_upload() so the two can't drift
    — and so "absent/unreadable data is unknown, never a negative verdict" is
    decided in one place.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        logger.info("Unparseable ISO timestamp %r — treating as unknown.", value)
        return None
    # A bare date (or any offsetless value) parses tz-NAIVE; coerce to UTC so
    # every caller can subtract it from datetime.now(timezone.utc) without an
    # aware/naive TypeError.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def channel_age_months(published_at: str | None) -> float | None:
    """
    Age of a channel in months, from the ISO 8601 timestamp channels.list
    returns in snippet.publishedAt.

    Returns None when the value is missing or unparseable — callers must
    treat None as "unknown" and NOT as "new", since absent data is not
    evidence against a channel.
    """
    created = _parse_iso_timestamp(published_at)
    if created is None:
        return None
    delta_days = (datetime.now(timezone.utc) - created).days
    return delta_days / DAYS_PER_MONTH


def days_since_last_upload(upload_dates: list[str]) -> float | None:
    """
    Days since the channel's most recent SAMPLED upload, from the ISO 8601
    timestamps get_recent_video_performance() returns in `upload_dates`.

    The NEWEST date is what counts, taken with max() rather than trusting the
    list's order: a channel that posted after a long gap is active, and a
    reordering of the sample must not change the verdict.

    Returns None when the list is empty or nothing parses — callers must
    treat None as "unknown" and NOT as "stale", the same rule
    channel_age_months() follows for an unknown publishedAt.
    """
    parsed = [dt for dt in (_parse_iso_timestamp(d) for d in upload_dates) if dt is not None]
    if not parsed:
        return None
    return (datetime.now(timezone.utc) - max(parsed)).days


def _view_count_is_settled(published_at: str | None) -> bool:
    """
    Whether a video has been public long enough (PERFORMANCE_MATURITY_DAYS)
    for its view count to feed the per-video floor.

    An unknown or unparseable publish date returns False: we can't confirm the
    count has settled, so the video isn't judged rather than judged on a value
    that may still be climbing — unknown never disqualifies.
    """
    published = _parse_iso_timestamp(published_at)
    if published is None:
        return False
    return (datetime.now(timezone.utc) - published).days >= PERFORMANCE_MATURITY_DAYS


def _as_int(value, default: int = 0) -> int:
    """
    int() that can't take a run down over one channel's statistics block.

    `stats.get("subscriberCount", 0)` looks safe but isn't: the default only
    applies when the key is ABSENT, and YouTube sometimes sends the key
    present with a JSON `null` (hidden/unreported counters). `.get()` then
    returns None, `int(None)` raises TypeError, and nothing up the call chain
    catches it — `process_candidate()` only handles a `None` return, so one
    channel with a null counter would end the whole niche. Unreadable means
    "use the default", never "crash".
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.info("Unparseable numeric statistic %r — using %d.", value, default)
        return default


def _json_or_none(resp, call_name: str):
    """
    `resp.json()` that can't take a run down over an unparseable 200 body.

    A 200 is not a promise of JSON. A corporate proxy, a captive portal, or a
    Google frontend error page all return HTML with a 200, and
    `requests.exceptions.JSONDecodeError` is raised for exactly that. Because
    it subclasses RequestException, the guards around the requests above look
    like they'd catch it — they don't, because `.json()` is called outside
    them, so it would propagate through process_candidate() ->
    push_until_full() -> run_niche() and kill the entire run.

    `do_not_contact.fetch_blocklist()` already treats a non-JSON 200 as a
    fetch failure for the same reason; this is the same defence applied on the
    enrichment side, where the right answer is to skip the channel (None)
    rather than abort the run.
    """
    try:
        return resp.json()
    except ValueError as e:
        # ValueError, not requests.exceptions.JSONDecodeError: the latter
        # subclasses both, and catching the builtin also covers a stdlib
        # json.JSONDecodeError if the body is decoded some other way.
        logger.warning("%s returned a 200 with a non-JSON body: %s", call_name, e)
        return None


def get_channel_stats(channel_id: str | None = None, *, handle: str | None = None) -> dict | None:
    """
    Fetch subscriberCount, videoCount, viewCount, country, title, and
    the uploads playlist ID for a channel.

    Identify the channel by EITHER its UC… `channel_id` (channels.list?id=)
    OR its `@handle` (channels.list?forHandle=) — exactly one is required.
    The handle path exists for influencer_discovery.py, which surfaces
    creators by @handle rather than by channel ID; resolving the id here
    piggybacks on the channels.list call enrichment makes anyway, so a
    discovery candidate costs no extra YouTube quota to bridge to an id.

    Quota cost: 1 unit (channels.list with part=snippet,statistics,contentDetails)
    — a flat 1 unit whether the lookup is by id or by forHandle.

    Returns None (and logs a warning) if the channel is private, deleted,
    or otherwise inaccessible — including unreachable, see the exception
    handler below — so callers can skip it without crashing. The returned
    "channel_id" is always read from the RESPONSE (item["id"]), which is what
    makes the forHandle path resolve to a real UC… id rather than echoing the
    handle back.
    """
    if bool(channel_id) == bool(handle):
        raise ValueError("get_channel_stats requires exactly one of channel_id or handle")

    # No "key" in params, here or at any other call site in this module: the
    # API key is set ONCE as the X-goog-api-key header on the shared session
    # in http_client.py. It must not be reintroduced — `requests` embeds the
    # full request URL in its exception messages, so a `key=` query parameter
    # gets printed verbatim into stdout by any unhandled network error, which
    # in CI is an Actions log retained for 90 days.
    params = {"part": "snippet,statistics,contentDetails"}
    if channel_id:
        params["id"] = channel_id
    else:
        # YouTube accepts the handle with or without the leading '@'; send it
        # with, since normalize_handle() strips it everywhere else.
        params["forHandle"] = f"@{handle}"
    ident = channel_id or f"@{handle}"
    try:
        resp = HTTP.get(f"{YOUTUBE_API_BASE_URL}/channels", params=params, timeout=30)
    except requests.RequestException as e:
        # By the time an exception surfaces here the session's retry adapter
        # has already burned all 5 attempts and ~45s of backoff (see
        # http_client.py), so this is a genuinely unreachable API rather than
        # a transient blip worth retrying again. Return None like any other
        # inaccessible channel: uncaught, a single ConnectionError/ReadTimeout
        # would unwind through process_candidate() -> push_until_full() ->
        # run_niche(), none of which catch it, and kill the entire run over
        # one bad channel.
        logger.warning("channels.list request failed for %s: %s", ident, e)
        return None

    if resp.status_code != 200:
        logger.warning("channels.list failed for %s: %s %s", ident, resp.status_code, safe_body(resp))
        return None

    # Charged only AFTER the status check, not right after the request: an
    # error response costs zero units on Google's side (a 403 quotaExceeded
    # most of all), so recording spend unconditionally inflated our own
    # quota_log and needlessly shrank the QUOTA_CEILING headroom the rest of
    # the run gets to use. Only a call that actually returned data is billed.
    record_spend(QUOTA_COST_CHANNELS_LIST, call_name=f"channels.list({ident})")

    payload = _json_or_none(resp, f"channels.list({ident})")
    if payload is None:
        return None

    items = payload.get("items", [])
    if not items:
        logger.warning("Channel %s not found (private, deleted, or terminated) — skipping.", ident)
        return None

    item = items[0]
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    content_details = item.get("contentDetails", {})
    uploads_playlist_id = content_details.get("relatedPlaylists", {}).get("uploads")

    # Read the id from the RESPONSE so a forHandle lookup resolves to the real
    # UC… id; fall back to the input for an id lookup whose mock/payload omits
    # it (preserves the pre-forHandle behaviour every existing caller relied on).
    resolved_id = item.get("id") or channel_id

    if stats.get("hiddenSubscriberCount"):
        logger.info("Channel %s has a hidden subscriber count.", ident)

    return {
        "channel_id": resolved_id,
        "channel_title": snippet.get("title"),
        "country": snippet.get("country", "Unknown"),
        # From the snippet already being fetched — no extra quota.
        "published_at": snippet.get("publishedAt", ""),
        "subscriber_count": _as_int(stats.get("subscriberCount")),
        "video_count": _as_int(stats.get("videoCount")),
        "view_count": _as_int(stats.get("viewCount")),
        "uploads_playlist_id": uploads_playlist_id,
        "description": snippet.get("description", ""),
        "business_email": extract_business_email(snippet.get("description", "")),
        # "" for legacy /c/ or /user/ channels that never set an @handle —
        # those can't be matched against the @handle-only external tables.
        "handle": normalize_handle(snippet.get("customUrl", "")),
    }


def get_recent_video_performance(
    channel_id: str,
    uploads_playlist_id: str,
    max_results: int = EMAIL_SCAN_SAMPLE_SIZE,
) -> dict | None:
    """
    Pull the last `max_results` videos from the channel's uploads playlist
    and fetch their stats.

    Two different window sizes come out of this one pair of calls:

    - Email scanning reads all `max_results` descriptions (default 50).
    - avg views / engagement rate / upload cadence are computed over only
      the newest PERFORMANCE_SAMPLE_SIZE (10) videos, so those figures —
      and the Overall Score built on them — stay comparable with records
      already in Airtable under the "Avg Views (last 10 videos)" column.

    Quota cost: 1 unit for playlistItems.list + 1 unit for videos.list
    (a single videos.list call handles up to 50 IDs at once, so this is
    always exactly 2 units regardless of max_results <= 50 — which is why
    scanning 50 descriptions instead of 10 is free).

    Returns None if the channel has no accessible uploads playlist (e.g.
    channel has zero public videos), or if either request can't be
    completed — a network failure skips the candidate, it doesn't end the
    run.
    """
    if not uploads_playlist_id:
        logger.warning("Channel %s has no uploads playlist — skipping video performance.", channel_id)
        return None

    # No "key" — it travels as a header on the shared session. See
    # get_channel_stats() above for why reintroducing it here is unsafe.
    params = {
        "part": "contentDetails",
        "playlistId": uploads_playlist_id,
        "maxResults": max_results,
    }
    try:
        resp = HTTP.get(f"{YOUTUBE_API_BASE_URL}/playlistItems", params=params, timeout=30)
    except requests.RequestException as e:
        # Retries are already exhausted at this point — see get_channel_stats().
        logger.warning("playlistItems.list request failed for %s: %s", channel_id, e)
        return None

    if resp.status_code != 200:
        logger.warning("playlistItems.list failed for %s: %s %s", channel_id, resp.status_code, safe_body(resp))
        return None

    # Spend recorded only for a call that returned data — see get_channel_stats().
    record_spend(QUOTA_COST_PLAYLIST_ITEMS_LIST, call_name=f"playlistItems.list({channel_id})")

    payload = _json_or_none(resp, f"playlistItems.list({channel_id})")
    if payload is None:
        return None

    items = payload.get("items", [])
    video_ids = [i["contentDetails"]["videoId"] for i in items if i.get("contentDetails", {}).get("videoId")]
    # playlistItems returns newest-first, so the leading slice is "the last
    # N videos" — the window upload cadence is measured over, matching the
    # performance window below rather than the wider email-scan window.
    upload_dates = [i["contentDetails"].get("videoPublishedAt") for i in items[:PERFORMANCE_SAMPLE_SIZE]]
    upload_dates = [d for d in upload_dates if d]

    if not video_ids:
        logger.warning("Channel %s uploads playlist has no videos — skipping video performance.", channel_id)
        return None

    time.sleep(API_SLEEP_SECONDS)

    # part=snippet adds no extra quota cost (videos.list is a flat 1 unit
    # regardless of parts requested) and lets us read defaultAudioLanguage,
    # a per-video signal for "Content Language" — channel-level language
    # isn't reliably exposed by the API at all.
    video_params = {
        # contentDetails adds `duration`, which is the only Shorts signal the
        # API offers. Free: videos.list is a flat 1 unit regardless of parts.
        "part": "snippet,statistics,contentDetails",
        "id": ",".join(video_ids),
    }
    try:
        video_resp = HTTP.get(f"{YOUTUBE_API_BASE_URL}/videos", params=video_params, timeout=30)
    except requests.RequestException as e:
        # Retries are already exhausted at this point — see get_channel_stats().
        # Note the playlistItems unit above is still (correctly) charged: that
        # call did return data.
        logger.warning("videos.list request failed for %s: %s", channel_id, e)
        return None

    if video_resp.status_code != 200:
        logger.warning("videos.list failed for %s: %s %s", channel_id, video_resp.status_code, safe_body(video_resp))
        return None

    # Spend recorded only for a call that returned data — see get_channel_stats().
    record_spend(QUOTA_COST_VIDEOS_LIST, call_name=f"videos.list({channel_id})")

    video_payload = _json_or_none(video_resp, f"videos.list({channel_id})")
    if video_payload is None:
        return None

    video_items = video_payload.get("items", [])
    if not video_items:
        return None

    # Select the performance window by video ID rather than by slicing
    # video_items, since videos.list gives no ordering guarantee — the
    # authoritative "newest first" order is the playlistItems one.
    performance_ids = set(video_ids[:PERFORMANCE_SAMPLE_SIZE])

    total_views = 0
    total_engagements = 0  # likes + comments
    performance_count = 0
    # Per-video views across the performance window, kept so the caller can
    # gate on "every recent video passed a view floor" — a stricter test than
    # avg_views, which one strong upload can carry over the line on its own.
    performance_views = []
    video_languages = []
    video_descriptions = []
    durations = []
    for v in video_items:
        snippet = v.get("snippet", {})
        # Every fetched video feeds the email scan — that's the whole
        # point of pulling EMAIL_SCAN_SAMPLE_SIZE of them.
        video_descriptions.append(snippet.get("description", ""))
        # Shorts detection reads the WIDE window (every fetched video, up to
        # EMAIL_SCAN_SAMPLE_SIZE) rather than the 10-video performance
        # window: more videos means more chances to see a long-form upload,
        # which makes a false "Shorts-only" verdict less likely. That matters
        # because that verdict discards the channel outright.
        durations.append(v.get("contentDetails", {}).get("duration"))
        # Collected across the WIDE window and reduced by dominant_language()
        # below, rather than taking the first non-empty tag: the value is now
        # gated on, and one mislabelled upload must not decide the channel.
        video_languages.append(
            snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage") or ""
        )

        # ...but only the newest PERFORMANCE_SAMPLE_SIZE feed the metrics.
        if v.get("id") not in performance_ids:
            continue
        stats = v.get("statistics", {})
        # _as_int, not int(): likeCount and commentCount are routinely absent
        # (disabled likes/comments) and can arrive as an explicit null, which
        # int() would turn into a TypeError mid-loop.
        raw_views = stats.get("viewCount")
        views = _as_int(raw_views)
        total_views += views
        total_engagements += _as_int(stats.get("likeCount")) + _as_int(stats.get("commentCount"))
        performance_count += 1
        # The per-video floor (min_views) judges only videos whose count has
        # SETTLED (public >= PERFORMANCE_MATURITY_DAYS) and is actually
        # REPORTED. A just-posted upload is still climbing toward 10k, and an
        # unreported count is unknown, not zero — counting either would sink
        # the channel on a value that isn't a real underperformer. avg_views
        # above is unchanged: it still spans the whole window.
        if raw_views is not None and _view_count_is_settled(snippet.get("publishedAt")):
            performance_views.append(views)

    if not performance_count:
        logger.warning("Channel %s returned no videos in the performance window — skipping.", channel_id)
        return None

    avg_views = total_views / performance_count
    # The weakest SETTLED, reported video in the window (see the append guard
    # above). None when no window video qualifies — e.g. a channel that just
    # posted its whole newest window — so the per-video floor is skipped
    # (unknown), not failed.
    min_views = min(performance_views) if performance_views else None
    avg_engagement_rate = (total_engagements / total_views * 100) if total_views > 0 else 0.0

    return {
        "avg_views": avg_views,
        # The lowest per-video views in the performance window — main's
        # per-video floor gates on this so a single weak recent upload isn't
        # hidden by a strong average.
        "min_views": min_views,
        "avg_engagement_rate": avg_engagement_rate,
        "upload_dates": upload_dates,
        # Size of the *performance* window (still 10), not the email scan.
        "sample_size": performance_count,
        # Most creators never set this, so it's frequently "" (Unknown) —
        # best-effort only, not a guaranteed signal. The MOST COMMON tag
        # across the wide window, not the newest video's.
        "content_language": dominant_language(video_languages),
        # True only if EVERY fetched video is <= SHORTS_MAX_SECONDS and none
        # had an unreadable duration. main.pre_push_drop_reason discards
        # these before any Airtable row is written.
        "shorts_only": is_shorts_only(durations),
        # Confirmed non-Shorts uploads in the wide window, and how many videos
        # that window actually held. main.pre_push_drop_reason needs BOTH:
        # "only 12 long-form" means something different when 12 of 12 videos
        # were sampled than when 12 of 50 were.
        "longform_count": count_longform(durations),
        "duration_sample_size": len(durations),
        # An email seen in EMAIL_MIN_VIDEO_REPEATS+ of the sampled videos'
        # descriptions — a stronger signal than a single mention anywhere.
        "repeated_email": find_repeated_email(video_descriptions),
        "video_descriptions": video_descriptions,
        # Where the newest-EMAIL_SCAN_SAMPLE_SIZE window ended, so
        # scan_older_videos_for_email() can continue from here instead of
        # re-fetching page 1 (2 wasted units) to find the same place.
        # "" when this channel has no older uploads.
        "next_page_token": payload.get("nextPageToken", ""),
    }


def scan_older_videos_for_email(
    channel_id: str,
    uploads_playlist_id: str,
    page_token: str,
    known_descriptions: list[str],
    max_pages: int = EMAIL_DEEP_SCAN_PAGES,
) -> str:
    """
    Keep paging back through a channel's uploads, looking for a contact
    email that the newest-EMAIL_SCAN_SAMPLE_SIZE window didn't turn up.

    Step 3 of the email chain (see main.resolve_email). Only worth running
    when steps 1-2 found nothing: channels that pivoted to Shorts often
    have terse recent descriptions while their older long-form videos
    still carry a "business inquiries" line.

    `known_descriptions` are the descriptions already scanned by step 1.
    Older descriptions are ADDED to those rather than counted on their
    own, so a channel with two recent mentions plus one older mention
    clears EMAIL_MIN_VIDEO_REPEATS — while a single mention anywhere
    still doesn't, which is the same bar step 1 applies.

    Quota cost: 2 units per page fetched (playlistItems.list +
    videos.list), and zero when `page_token` is empty or max_pages is 0.
    Stops as soon as an email clears the threshold, so a channel that
    answers on the first extra page never pays for the second.

    Every failure is soft — a bad page, or an unreachable API, returns ""
    rather than raising, matching the rest of this module. This step is a
    best-effort bonus on top of two free ones; nothing about it is worth
    ending a run for.
    """
    if not uploads_playlist_id or not page_token or max_pages <= 0:
        return ""

    descriptions = list(known_descriptions)

    for _ in range(max_pages):
        # No "key" — it travels as a header on the shared session. See
        # get_channel_stats() for why reintroducing it here is unsafe.
        params = {
            "part": "contentDetails",
            "playlistId": uploads_playlist_id,
            "maxResults": EMAIL_SCAN_SAMPLE_SIZE,
            "pageToken": page_token,
        }
        try:
            resp = HTTP.get(f"{YOUTUBE_API_BASE_URL}/playlistItems", params=params, timeout=30)
        except requests.RequestException as e:
            # Retries are already exhausted at this point — see get_channel_stats().
            logger.info(
                "Older-uploads page request failed for %s: %s — stopping the email scan here.",
                channel_id, e,
            )
            return ""

        if resp.status_code != 200:
            logger.info(
                "Older-uploads page failed for %s: %s — stopping the email scan here.",
                channel_id, resp.status_code,
            )
            return ""

        # Spend recorded only for a call that returned data — see get_channel_stats().
        record_spend(QUOTA_COST_PLAYLIST_ITEMS_LIST, call_name=f"playlistItems.list({channel_id}, older)")

        payload = _json_or_none(resp, f"playlistItems.list({channel_id}, older)")
        if payload is None:
            return ""

        video_ids = [
            i["contentDetails"]["videoId"]
            for i in payload.get("items", [])
            if i.get("contentDetails", {}).get("videoId")
        ]
        if not video_ids:
            return ""

        time.sleep(API_SLEEP_SECONDS)

        video_params = {
            "part": "snippet",
            "id": ",".join(video_ids),
        }
        try:
            video_resp = HTTP.get(f"{YOUTUBE_API_BASE_URL}/videos", params=video_params, timeout=30)
        except requests.RequestException as e:
            # Retries are already exhausted at this point — see get_channel_stats().
            logger.info(
                "Older-uploads videos.list request failed for %s: %s — stopping the email scan here.",
                channel_id, e,
            )
            return ""

        if video_resp.status_code != 200:
            logger.info(
                "Older-uploads videos.list failed for %s: %s — stopping the email scan here.",
                channel_id, video_resp.status_code,
            )
            return ""

        # Spend recorded only for a call that returned data — see get_channel_stats().
        record_spend(QUOTA_COST_VIDEOS_LIST, call_name=f"videos.list({channel_id}, older)")

        video_payload = _json_or_none(video_resp, f"videos.list({channel_id}, older)")
        if video_payload is None:
            return ""

        descriptions.extend(
            v.get("snippet", {}).get("description", "")
            for v in video_payload.get("items", [])
        )

        email = find_repeated_email(descriptions)
        if email:
            logger.info(
                "Found %s for %s after scanning %d descriptions (older uploads).",
                email, channel_id, len(descriptions),
            )
            return email

        page_token = payload.get("nextPageToken", "")
        if not page_token:
            return ""

        time.sleep(API_SLEEP_SECONDS)

    return ""


def count_longform_in_older_videos(
    channel_id: str,
    uploads_playlist_id: str,
    page_token: str,
    already_counted: int,
    target: int,
    max_pages: int = LONGFORM_SCAN_MAX_PAGES,
) -> int:
    """
    Keep paging back through a channel's uploads counting confirmed non-Shorts
    videos, until `target` is reached, the pages run out, or `max_pages` is
    spent. Returns the total (including `already_counted`).

    WHY this pages at all. The newest-EMAIL_SCAN_SAMPLE_SIZE window is a poor
    sample for "does this channel have 30+ long-form videos", because a
    channel that recently leaned into Shorts shows a Shorts-heavy newest 50
    while holding hundreds of long-form uploads behind it. Measured over 47
    otherwise-qualifying Home Theater candidates (2026-08-11), 29 already
    show 30+ long-form in the newest 50 and pay nothing here; of the 18 that
    don't, six are genuine mixed-format channels with large catalogues
    (WorkshopAddict 22/50 across 2,418 uploads, Kat Viana 23/50 across 471,
    Fat Hog Woodworking 25/50 across 199) that a newest-50-only rule would
    wrongly discard.

    Quota cost: 2 units per page (playlistItems.list + videos.list), and zero
    when `page_token` is empty, `max_pages` is 0, or the target is already
    met. The cap is what makes a Shorts factory cheap to reject rather than
    expensive to prove: at 200 videos examined, a channel needs roughly a 15%
    long-form rate to reach 30, so Dadrianca (2 of 50) is dropped after the
    cap instead of being paged through 791 uploads.

    Failures are soft, like the rest of this module: an unreachable page
    returns what has been counted so far rather than raising. That leans
    toward DISCARDING the channel (the count stays below target), which is
    the same direction count_longform() leans for an unreadable duration —
    the burden of proof is on the channel.
    """
    total = already_counted
    if total >= target or not uploads_playlist_id or not page_token or max_pages <= 0:
        return total

    for _ in range(max_pages):
        # No "key" — it travels as a header on the shared session. See
        # get_channel_stats() for why reintroducing it here is unsafe.
        params = {
            "part": "contentDetails",
            "playlistId": uploads_playlist_id,
            "maxResults": EMAIL_SCAN_SAMPLE_SIZE,
            "pageToken": page_token,
        }
        try:
            resp = HTTP.get(f"{YOUTUBE_API_BASE_URL}/playlistItems", params=params, timeout=30)
        except requests.RequestException as e:
            # Retries are already exhausted at this point — see get_channel_stats().
            logger.info(
                "Older-uploads page request failed for %s: %s — stopping the long-form count at %d.",
                channel_id, e, total,
            )
            return total

        if resp.status_code != 200:
            logger.info(
                "Older-uploads page failed for %s: %s — stopping the long-form count at %d.",
                channel_id, resp.status_code, total,
            )
            return total

        # Spend recorded only for a call that returned data — see get_channel_stats().
        record_spend(QUOTA_COST_PLAYLIST_ITEMS_LIST, call_name=f"playlistItems.list({channel_id}, longform)")

        payload = _json_or_none(resp, f"playlistItems.list({channel_id}, longform)")
        if payload is None:
            return total

        video_ids = [
            i["contentDetails"]["videoId"]
            for i in payload.get("items", [])
            if i.get("contentDetails", {}).get("videoId")
        ]
        if not video_ids:
            return total

        time.sleep(API_SLEEP_SECONDS)

        # contentDetails only — the duration is the sole thing needed here,
        # and the call is a flat 1 unit regardless of the parts requested.
        video_params = {"part": "contentDetails", "id": ",".join(video_ids)}
        try:
            video_resp = HTTP.get(f"{YOUTUBE_API_BASE_URL}/videos", params=video_params, timeout=30)
        except requests.RequestException as e:
            logger.info(
                "Older-uploads videos.list request failed for %s: %s — stopping the long-form count at %d.",
                channel_id, e, total,
            )
            return total

        if video_resp.status_code != 200:
            logger.info(
                "Older-uploads videos.list failed for %s: %s — stopping the long-form count at %d.",
                channel_id, video_resp.status_code, total,
            )
            return total

        record_spend(QUOTA_COST_VIDEOS_LIST, call_name=f"videos.list({channel_id}, longform)")

        video_payload = _json_or_none(video_resp, f"videos.list({channel_id}, longform)")
        if video_payload is None:
            return total

        total += count_longform(
            v.get("contentDetails", {}).get("duration")
            for v in video_payload.get("items", [])
        )
        if total >= target:
            return total

        page_token = payload.get("nextPageToken", "")
        if not page_token:
            return total

        time.sleep(API_SLEEP_SECONDS)

    return total


def calc_upload_frequency(upload_dates: list[str]) -> float:
    """
    Estimate videos-per-month upload cadence from a list of ISO 8601
    upload timestamps (as returned by playlistItems.list).

    Uses the span between the oldest and newest sampled video, so this is
    a local estimate over the sampled window (not full channel history).
    """
    if not upload_dates or len(upload_dates) < 2:
        return 0.0

    parsed = sorted(
        datetime.strptime(d, "%Y-%m-%dT%H:%M:%SZ") for d in upload_dates
    )
    span_days = (parsed[-1] - parsed[0]).days
    if span_days <= 0:
        # All sampled videos published on the same day; can't extrapolate a
        # monthly cadence from a zero-width window.
        return float(len(parsed))

    videos_per_day = len(parsed) / span_days
    return round(videos_per_day * 30, 2)
