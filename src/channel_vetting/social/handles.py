"""
Handle normalisation for TikTok and Instagram.

WHY THIS EXISTS RATHER THAN REUSING enrichment.channels.normalize_handle:
that function is built around YouTube's @handle and returns "" for anything
without a literal "@" — measured:

    normalize_handle("@petlover")                      -> "petlover"
    normalize_handle("petlover")                       -> ""        <-- !
    normalize_handle("https://instagram.com/petlover") -> ""        <-- !

influencers.club returns `profile.username` BARE, and Instagram profile URLs
carry no "@" at all. `InfluencerDiscovery._to_candidate()` drops any candidate
whose handle normalises to empty, so reusing the YouTube normaliser here would
have silently discarded EVERY Instagram creator and every TikTok creator whose
username arrived without an "@" — no error, no warning, just an empty run.

The YouTube normaliser is deliberately left alone: returning "" for a legacy
/c/ or /user/ channel is load-bearing for the YouTube dedupe and DO NOT CONTACT
indexes, so widening it to accept bare strings would change which YouTube
channels those indexes match.
"""
import re
from urllib.parse import urlparse

# TikTok and Instagram usernames: letters, digits, underscore, period. Both
# platforms also allow a trailing period to be dropped, and neither is
# case-sensitive, so everything is lowercased for indexing.
_SOCIAL_HANDLE = re.compile(r"[A-Za-z0-9_.]+")

# "instagram.com/foo" with no scheme. Requires a dot-TLD followed by a slash,
# so a handle that merely contains a dot ("corgi.daily") is NOT treated as a
# host and keeps its dot.
_LOOKS_LIKE_HOST = re.compile(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}/")

# Instagram/TikTok URL path prefixes that are NOT a profile. A url beginning
# with one of these carries no handle, so the only honest answer is "".
_RESERVED_PATHS = frozenset({
    "p", "reel", "reels", "tv", "explore", "stories", "s",
    "accounts", "direct", "about", "legal", "developer",
    "tag", "tags", "discover", "music", "search", "foryou", "video", "t",
})

# Templates live in social.platforms; imported lazily inside profile_url() to
# keep this module free of an import cycle (platforms imports nothing).


def normalize_social_handle(raw: str) -> str:
    """
    A bare, lowercased handle from a username, an @handle, or a profile URL.
    Returns "" when nothing usable is present.

    Accepts every shape the vendor and our own tables produce:
        "petlover", "@petlover", "PetLover",
        "https://www.tiktok.com/@petlover",
        "https://www.instagram.com/petlover/",
        "instagram.com/petlover?igsh=..."
    """
    if not raw:
        return ""
    text = str(raw).strip()
    if not text:
        return ""

    # A URL is reduced to its last PATH segment, via urlparse rather than a
    # split on "/". An earlier version picked "the last segment without a dot"
    # and returned "https" for the bare "https://www.instagram.com/" — the
    # scheme segment survived its own filter. Parsing means the host and scheme
    # are never candidates in the first place.
    if "://" in text or _LOOKS_LIKE_HOST.match(text):
        if "://" not in text:
            text = "https://" + text
        path = urlparse(text).path or ""
        segments = [s for s in path.split("/") if s]
        if not segments:
            return ""
        # An "@" segment wins over position. A TikTok POST url is
        # /@handle/video/123, so taking the last segment would return the video
        # id — and it would look like a perfectly valid handle downstream.
        at_segments = [s for s in segments if s.startswith("@")]
        if at_segments:
            text = at_segments[0]
        else:
            # An Instagram POST url (/p/<shortcode>/, /reel/<id>/) contains no
            # handle at all. Returning the shortcode would manufacture a
            # plausible-looking handle for a creator we cannot actually
            # identify, and it would then be deduped and written as if real.
            # Refuse instead.
            if segments[0].lower() in _RESERVED_PATHS:
                return ""
            text = segments[-1]

    text = text.lstrip("@")
    match = _SOCIAL_HANDLE.match(text)
    if not match:
        return ""
    return match.group(0).strip(".").lower()


def profile_url(platform: str, handle: str) -> str:
    """
    The canonical profile URL for a bare handle, or "" if either is unusable.

    Written from the handle rather than taken from the vendor so the value in
    Airtable is machine-generated. The criteria work has a reviewer clicking
    straight through to judge photo quality, so a wrong or tracking-laden URL
    costs review time.
    """
    from channel_vetting.social import platforms

    bare = normalize_social_handle(handle)
    if not bare:
        return ""
    try:
        template = platforms.spec(platform)["profile_url"]
    except ValueError:
        return ""
    return template.format(handle=bare)
