"""
The geographic search zones a prospect channel has to sit inside:
US, Canada, the UK, Europe, and Australia — Ireland excluded.

Why this is a channel-location filter and not a search parameter:
`search.list` does take a `regionCode`, but it takes exactly ONE per call
and it biases result *ranking* for viewers in that region rather than
restricting results to creators based there. Covering these zones that way
would mean re-running every keyword once per region — 100 quota units each,
roughly 30x the current discovery spend — and still wouldn't guarantee the
creator's own location. So the zone check happens after `channels.list`,
against the channel's declared country, where it costs nothing.

Two forms of that country reach this module, because two sources report it:

  - `channels.list` -> `snippet.country`, an ISO 3166-1 alpha-2 code ("US").
    Measured over the 34 channels in the live niche tables, 29 (85%) set it.
  - The region subtag of the content language ("en-GB" -> GB), for the
    other 15%. Free — `enrichment.get_recent_video_performance()` already
    reads `defaultAudioLanguage` for the "Content Language" column.

`zone_verdict()` takes either form; `region_from_language_tag()` converts
the second into the first.

Things measured and REJECTED as region sources, so they don't get tried
again:

  - **The About panel's country.** `aboutChannelViewModel.country` is real
    and does return a name, but it is the SAME channel setting the API
    exposes — the panel just renders it. All 5 live channels with an empty
    `snippet.country` had no `country` key in the About payload either:
    0 recovered, one page load each. (The control, a channel whose API
    country was `US`, did return "United States", which is how we know the
    lookup itself worked.)
  - **Bare language as a country.** Every `hi`/`ta` channel in the live
    tables already reports `country: IN`, so mapping language to country
    would have added nothing — while being wrong in the other direction for
    4 of the 29 channels that do declare a country: `en-US` content from
    India, Austria and Serbia, and `fr-FR` content from the US. Language
    describes the audience, not the creator's location, which is why only
    the explicit REGION SUBTAG is used and only when no country is known.
"""
import re

# Ireland is deliberately absent even though it is both an EU member and
# the obvious neighbour of the UK zone — "UK (except Ireland)" was an
# explicit instruction, so IE is excluded from every zone here, not just
# from the UK one. Removing it from this comment's reasoning first, then
# the set, is the order to undo it in.
ALLOWED_COUNTRY_CODES = frozenset({
    # North America
    "US", "CA",
    # United Kingdom (GB covers England, Scotland, Wales and Northern
    # Ireland; the Republic of Ireland is IE and is excluded).
    "GB",
    # Australia
    "AU",
    # European Union, minus IE
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI",
    "ES", "SE",
    # EFTA / EEA
    "IS", "LI", "NO", "CH",
    # European microstates
    "AD", "MC", "SM", "VA",
    # Western Balkans and eastern Europe
    "AL", "BA", "ME", "MK", "RS", "XK", "MD", "UA",
})

# Deliberately NOT in the set above, flagged here so the omission reads as
# a decision rather than an oversight: RU, BY and TR are geographically
# European in whole or part. Add them to ALLOWED_COUNTRY_CODES if the
# brief's "Europe" is meant to stretch that far.

# The About panel renders a country NAME, so names need their own lookup.
# Keys are lowercased and stripped; aliases matter because the panel's
# wording varies by YouTube locale and by era ("Czechia" vs "Czech
# Republic", "Holland" vs "Netherlands").
ALLOWED_COUNTRY_NAMES = {
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "us": "US",
    "america": "US",
    "canada": "CA",
    "united kingdom": "GB",
    "united kingdom of great britain and northern ireland": "GB",
    "great britain": "GB",
    "britain": "GB",
    "uk": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    "australia": "AU",
    "austria": "AT",
    "belgium": "BE",
    "bulgaria": "BG",
    "croatia": "HR",
    "cyprus": "CY",
    "czechia": "CZ",
    "czech republic": "CZ",
    "denmark": "DK",
    "estonia": "EE",
    "finland": "FI",
    "france": "FR",
    "germany": "DE",
    "greece": "GR",
    "hungary": "HU",
    "italy": "IT",
    "latvia": "LV",
    "lithuania": "LT",
    "luxembourg": "LU",
    "malta": "MT",
    "netherlands": "NL",
    "the netherlands": "NL",
    "holland": "NL",
    "poland": "PL",
    "portugal": "PT",
    "romania": "RO",
    "slovakia": "SK",
    "slovenia": "SI",
    "spain": "ES",
    "sweden": "SE",
    "iceland": "IS",
    "liechtenstein": "LI",
    "norway": "NO",
    "switzerland": "CH",
    "andorra": "AD",
    "monaco": "MC",
    "san marino": "SM",
    "vatican city": "VA",
    "holy see": "VA",
    "albania": "AL",
    "bosnia and herzegovina": "BA",
    "montenegro": "ME",
    "north macedonia": "MK",
    "macedonia": "MK",
    "serbia": "RS",
    "kosovo": "XK",
    "moldova": "MD",
    "ukraine": "UA",
}

# Countries the pipeline actually keeps surfacing that sit OUTSIDE the
# zones. This list exists so an About-panel name can produce a confident
# "outside" verdict rather than an "unknown" one — see zone_verdict()'s
# note on why an unrecognised name is treated as unknown instead. It does
# not have to be exhaustive; anything missing simply falls through to
# unknown and gets reviewed by a human.
KNOWN_OUTSIDE_COUNTRY_NAMES = {
    # Explicitly excluded by the brief, and the one name most likely to be
    # mistaken for an allowed zone.
    "ireland": "IE",
    "republic of ireland": "IE",
    # Asia
    "india": "IN", "pakistan": "PK", "bangladesh": "BD", "sri lanka": "LK",
    "nepal": "NP", "philippines": "PH", "indonesia": "ID", "malaysia": "MY",
    "singapore": "SG", "thailand": "TH", "vietnam": "VN", "viet nam": "VN",
    "cambodia": "KH", "myanmar": "MM", "china": "CN", "hong kong": "HK",
    "taiwan": "TW", "japan": "JP", "south korea": "KR", "korea": "KR",
    "kazakhstan": "KZ", "uzbekistan": "UZ", "afghanistan": "AF",
    # Middle East and Africa
    "united arab emirates": "AE", "saudi arabia": "SA", "qatar": "QA",
    "kuwait": "KW", "israel": "IL", "jordan": "JO", "lebanon": "LB",
    "iraq": "IQ", "iran": "IR", "turkey": "TR", "turkiye": "TR",
    "egypt": "EG", "morocco": "MA", "algeria": "DZ", "tunisia": "TN",
    "nigeria": "NG", "ghana": "GH", "kenya": "KE", "uganda": "UG",
    "tanzania": "TZ", "ethiopia": "ET", "south africa": "ZA",
    # Latin America
    "mexico": "MX", "brazil": "BR", "brasil": "BR", "argentina": "AR",
    "chile": "CL", "colombia": "CO", "peru": "PE", "venezuela": "VE",
    "ecuador": "EC", "bolivia": "BO", "uruguay": "UY", "paraguay": "PY",
    "guatemala": "GT", "dominican republic": "DO", "costa rica": "CR",
    "puerto rico": "PR", "cuba": "CU", "panama": "PA", "honduras": "HN",
    # Elsewhere
    "russia": "RU", "russian federation": "RU", "belarus": "BY",
    "new zealand": "NZ",
}

# What channels.list puts in snippet.country when a channel set nothing —
# get_channel_stats() defaults it to "Unknown" rather than leaving it "".
UNKNOWN_COUNTRY_VALUES = {"", "unknown", "none", "null", "n/a", "-"}


def _normalize_name(raw: str) -> str:
    return " ".join(raw.strip().lower().replace(",", " ").split())


def region_from_language_tag(raw: str | None) -> str:
    """
    The ISO 3166-1 alpha-2 REGION subtag of a BCP-47 content-language tag
    — "en-GB" -> "GB", "zh-Hant-TW" -> "TW" — or "" when the tag carries no
    region ("en", "ta", "hi").

    A bare language is deliberately NOT mapped to a country. `ta` is spoken
    in India, Sri Lanka and Singapore; `en`, `es`, `fr`, `ar` and `pt` each
    span both sides of the zone boundary. Measured on the live tables, a
    bare-language mapping would have added nothing (every `hi`/`ta` channel
    already declared `country: IN`) while being wrong about 4 channels that
    did declare one.

    Numeric UN M.49 regions ("en-419", Latin America) return "" too: they
    aren't alpha-2 codes and cover several countries at once.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    # Subtag 0 is the language; a region is the first 2-letter subtag after
    # it. Anything longer in between is a script ("Hant") or a variant.
    for subtag in text.replace("_", "-").split("-")[1:]:
        if len(subtag) == 2 and subtag.isalpha():
            return subtag.upper()
    return ""


def country_code(raw: str | None) -> str:
    """
    ISO 3166-1 alpha-2 code for whatever the caller has — an API country
    code, or a country name off the About panel — or "" when it can't be
    resolved to one.

    "" means genuinely unresolved. A code this function does not recognise
    as a name (e.g. "IN") still comes back as "IN": resolving a country is
    a separate question from whether that country is in a search zone.
    """
    text = (raw or "").strip()
    if not text or text.lower() in UNKNOWN_COUNTRY_VALUES:
        return ""
    if len(text) == 2 and text.isalpha():
        return text.upper()

    name = _normalize_name(text)
    return ALLOWED_COUNTRY_NAMES.get(name) or KNOWN_OUTSIDE_COUNTRY_NAMES.get(name, "")


def zone_verdict(raw: str | None) -> bool | None:
    """
    Whether a channel's declared country is inside the allowed search
    zones:

      True  — declared, and inside.
      False — declared, and outside.
      None  — nothing declared, or a name this module doesn't recognise.

    None is NOT "outside". An unrecognised country name is absent data,
    and this project's standing rule is that absent data is not evidence
    against a channel (same treatment as an unknown channel age or a
    hidden subscriber count) — so the caller keeps it and a human decides.
    The alternative, reading "a name I don't have in my table" as "outside
    the zones", would silently discard real prospects every time YouTube
    changes a label or renders the panel in another locale.

    If a country genuinely outside the zones keeps slipping through as
    unknown, add its name to KNOWN_OUTSIDE_COUNTRY_NAMES — don't invert
    this default.
    """
    code = country_code(raw)
    if not code:
        return None
    return code in ALLOWED_COUNTRY_CODES


# Cues a creator uses to state where they actually LIVE, so a non-zone
# country right after one is their location rather than a passing mention.
# "from" is deliberately NOT a cue: "from India" too often means "parents
# from India" or "clips from India", and "shot in"/"filmed in" describe a
# location the video was made, not the creator's residence.
_LOCATION_CUE = r"(?:based in|based out of|located in|living in|lives in|home base|📍|location\s*[:\-])"
# Longest names first so "south korea" wins over "korea", "hong kong" over a
# stray "kong", etc.
_OUTSIDE_NAME_ALTERNATION = "|".join(
    re.escape(n) for n in sorted(KNOWN_OUTSIDE_COUNTRY_NAMES, key=len, reverse=True)
)
_DESC_LOCATION_PATTERN = re.compile(
    _LOCATION_CUE + r"[^\n,.]{0,30}?\b(" + _OUTSIDE_NAME_ALTERNATION + r")\b",
    re.IGNORECASE,
)


def description_location_outside_zone(description: str | None) -> str:
    """
    The ISO code of a non-zone country a channel's About description gives as
    its LOCATION (e.g. "based in the Philippines" -> "PH"), or "".

    Catches creators who set snippet.country to the US but reveal their real,
    outside-the-zone location in their description. An explicit location cue
    is required before the country name, so a passing mention ("gear from
    Japan", "shot in Iceland") does not trip it, and only OUTSIDE names are
    matched, so an in-zone location ("based in Canada") never fires. It is a
    heuristic: a location stated only as a city or a flag emoji is missed, but
    the burden is on excluding, so a miss just means a human might review one.
    """
    if not description:
        return ""
    match = _DESC_LOCATION_PATTERN.search(description)
    # group(1) is captured from an alternation of already-normalized keys, with
    # the \b anchors outside the capture, so it can't carry surrounding
    # whitespace — only case differs (the pattern is IGNORECASE), so .lower()
    # is enough to hit the (lowercase) KNOWN_OUTSIDE_COUNTRY_NAMES key.
    return KNOWN_OUTSIDE_COUNTRY_NAMES[match.group(1).lower()] if match else ""
