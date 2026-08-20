"""
The geographic search zones a prospect channel has to sit inside.

**2026-08-20: both niches now run on US / Canada / UK / Australia only.**
Europe was removed at the operator's direction ("Europe is not in our search
zone for lifestyle. Only UK USA CANADA and AUS"), and the standing instruction
to unify the two niches' criteria — the same one that moved Lifestyle Sofa's
view floor from 2,000 to 10,000 — carries it to Home Theater as well. The
European block is still DEFINED below as EUROPE_COUNTRY_CODES so restoring it
to a niche is a one-line edit of that niche's `allowed_country_codes`, not
forty codes retyped from memory.

Why this is a channel-location filter and not a search parameter:
`search.list` does take a `regionCode`, but it takes exactly ONE per call
and it biases result *ranking* for viewers in that region rather than
restricting results to creators based there. Covering these zones that way
would mean re-running every keyword once per region — 100 quota units each,
roughly 30x the current discovery spend — and still wouldn't guarantee the
creator's own location. So the zone check happens after `channels.list`,
against the channel's declared country, where it costs nothing.

**The declared country is now REQUIRED, and it is not trusted on its own.**
Two changes on 2026-08-20, both from one measurement over all 144 rows
already tracked in the two niche tables:

  - `snippet.country` must be SET and in the niche's zone. It used to be
    treated as absent data and kept (`zone_verdict` -> None -> keep), with the
    content language's region subtag standing in when it was blank. That
    fallback is deleted: `en-US` describes the AUDIENCE, and the live tables
    hold Vietnamese, Kenyan and Japanese creators that reached them that way.
    Requiring the field costs 8 of 144 rows (5.6%) — far less than the 15%
    the old comment here predicted.
  - Requiring it is NOWHERE NEAR SUFFICIENT, which is the more important
    finding. YouTube's country field is self-declared and unverified, and
    creators set it to their MARKET. Measured: `Linet_ke` ("a girl from a
    village in Kenya Africa") declares US; `Olesya & house` ("I live in
    Belarus") declares US; `Thai Girl Gift & Foreigner Joe` ("We live in ...
    Thailand") declares US; `LIV KENYA` declares GB. NINE of the twelve
    genuinely out-of-zone rows declare an IN-ZONE country. So three
    title/description signals below now OVERRIDE a declared country.

Signals, highest precedence first. All are free — they read the title and the
About description that `channels.list` has already returned:

  1. `flag_country_outside_zone()` — a flag emoji in the channel TITLE.
     A regional-indicator pair literally encodes an ISO code, so this is
     exact rather than heuristic.
  2. `title_country_outside_zone()` — an out-of-zone country NAME in the
     channel title ("LIV KENYA", "Inside Japan Living").
  3. `description_location_outside_zone()` — an explicit location cue plus an
     out-of-zone country name in the About text.
  4. `zone_verdict()` on the declared `snippet.country`.

Measured over all 144 tracked rows, signals 1-3 fire on 7 channels and every
one is a true positive; zero false positives. Each function records the
variants that were tried and rejected — read those before widening one.

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
    describes the audience, not the creator's location.
  - **The content language's REGION SUBTAG** ("en-GB" -> GB), which used to
    stand in for a missing country and is now gone entirely — see above.
    `region_from_language_tag()` survives as a pure helper, because the full
    tag is still written to the "Content Language" column verbatim (which is
    why that tag must never be normalised to a bare "en"), but NOTHING reads
    it as a location any more. Do not re-wire it.
  - **A bare country name anywhere in the About text**, with no location cue
    in front of it. Measured over the same 144 rows it fires 11 times and at
    least two are plainly wrong: "jordan" matched a person's name in
    `Emily Canham`'s bio, and a passing "afghanistan" matched in `Kresnt`'s.
    The cue requirement is what makes signal 3 precise.
"""
import re

# The zone BOTH niches run on since 2026-08-20 (see the module docstring).
# Ireland is deliberately absent even though it is the obvious neighbour of
# the UK zone — "UK (except Ireland)" was an explicit instruction, so IE is
# excluded from every zone here, not just from the UK one. Removing it from
# this comment's reasoning first, then the set, is the order to undo it in.
ZONE_CORE = frozenset({
    # North America
    "US", "CA",
    # United Kingdom (GB covers England, Scotland, Wales and Northern
    # Ireland; the Republic of Ireland is IE and is excluded).
    "GB",
    # Australia
    "AU",
})

# Europe, kept DEFINED but WIRED TO NOTHING as of 2026-08-20: no niche's
# `allowed_country_codes` includes it. It stays here so that restoring Europe
# to a niche is `ZONE_CORE | EUROPE_COUNTRY_CODES` in niches.py rather than a
# fresh and error-prone list — and so the deliberate omissions recorded below
# (IE, and RU/BY/TR) are not lost along with it.
EUROPE_COUNTRY_CODES = frozenset({
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
# European in whole or part. Add them to EUROPE_COUNTRY_CODES if a "Europe"
# zone is ever restored to a niche AND is meant to stretch that far.

# The widest zone this module knows about: every code that could legitimately
# appear in SOME niche's zone. It is the default argument for `zone_verdict()`
# and the anchor for the name-table invariants below — it is NOT what any
# niche actually runs on today. Niches pass their own `allowed_country_codes`,
# and `REQUIRED_NICHE_KEYS` in main.py makes that key mandatory precisely so a
# niche cannot silently inherit this wider default.
ALLOWED_COUNTRY_CODES = ZONE_CORE | EUROPE_COUNTRY_CODES

# influencers.club's OWN spelling of a country, for the server-side `location`
# discovery filter. Keyed on our country code so the filter is derived from the
# niche's `allowed_country_codes` and cannot drift from the local zone gate.
#
# EVERY NAME HERE IS VERIFIED LIVE against the vendor's discovery endpoint, and
# that is the entry requirement for this table — not a guess at their spelling.
# Verified 2026-08-20 (Home Theater / Lifestyle Sofa totals, one name per
# request at limit=1):
#
#     United States ....  544 / 2199
#     Canada ..........   64 /  254
#     United Kingdom ..  100 /  443
#     Australia .......   26 /  106
#     all four ........  734 / 3002   <- exactly the sum of the singles
#
# The sum matching the combined total is the check that matters: it proves the
# filter PARTITIONS the pool rather than being silently ignored. The vendor also
# accepts an `average_views` filter and ignores it completely (total unchanged at
# min=100,000,000), so "the request returned 200" is not evidence a filter works.
# An invalid name is a hard 400 ("Invalid location: 'Atlantis' is not a valid
# location"), which is why an UNVERIFIED name must never be added here: a 400
# fails the whole discovery request, i.e. zero rows, where a missing filter only
# costs credits.
#
# EUROPE IS DELIBERATELY ABSENT. No niche admits it as of 2026-08-20 (see
# EUROPE_COUNTRY_CODES above), and guessing 30 vendor spellings would trade a
# credit leak for a run-killing 400. If Europe is restored to a niche,
# vendor_locations_for() below returns nothing for it and the filter is skipped
# with a warning rather than sent half-populated — verify the names, add them
# here, and the filter switches itself back on.
VENDOR_LOCATION_NAMES = {
    "US": "United States",
    "CA": "Canada",
    "GB": "United Kingdom",
    "AU": "Australia",
}


def vendor_locations_for(allowed_codes) -> list[str]:
    """
    The vendor `location` values for a niche's zone, or [] if any code is unmapped.

    ALL-OR-NOTHING, and that is the whole point. Sending the subset we happen to
    have names for would silently EXCLUDE creators the niche allows — a zone of
    {US, CA, GB, AU, DE} with no "DE" name would filter German creators out
    server-side while the local gate still admitted them, and the only symptom
    would be a quieter table. Returning [] instead leaves the filter off, which
    is the previous behaviour: it costs credits on out-of-zone creators that
    location_drop_reason() then discards, and costs no reachable prospect.

    So the failure direction is deliberately the expensive one, not the lossy one.
    """
    codes = sorted(allowed_codes or ())
    if not codes:
        return []
    names = [VENDOR_LOCATION_NAMES.get(code) for code in codes]
    if any(name is None for name in names):
        return []
    # dict.fromkeys, not set(), so the order is stable for tests and log lines.
    return list(dict.fromkeys(names))

# The About panel renders a country NAME, so names need their own lookup.
# Keys are lowercased and stripped; aliases matter because the panel's
# wording varies by YouTube locale and by era ("Czechia" vs "Czech
# Republic", "Holland" vs "Netherlands").
#
# NOTE THE NAME IS NOW HISTORICAL. "Allowed" here means "in ALLOWED_COUNTRY_CODES",
# i.e. the WIDEST zone this module knows — not "allowed by a niche". Since
# 2026-08-20 no niche admits the European entries below. This table's real job
# is name -> code RESOLUTION for `country_code()`; whether that code is in zone
# is `zone_verdict()`'s question, and it asks it against the niche's own set.
# It is kept split from KNOWN_OUTSIDE_COUNTRY_NAMES rather than merged because
# only the OUTSIDE half feeds the title and description signals, which must
# never vote "inside".
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


def zone_verdict(raw: str | None, allowed_codes=ALLOWED_COUNTRY_CODES) -> bool | None:
    """
    Whether a channel's declared country is inside `allowed_codes`:

      True  — declared, and inside.
      False — declared, and outside.
      None  — nothing declared, or a name this module doesn't recognise.

    `allowed_codes` is the NICHE's zone and callers are expected to pass it.
    The default is the widest zone this module knows (see
    ALLOWED_COUNTRY_CODES) and exists for tests and for the name-table
    invariants, NOT as a niche's fallback — `allowed_country_codes` is a
    required key in NICHES for exactly that reason, so a niche that forgets it
    is skipped with a logged error rather than quietly getting Europe back.

    **A None verdict is now a DROP, not a keep** (changed 2026-08-20). What
    this function reports is unchanged; what changed is
    `main.process_candidate`, which used to discard only on `is False` and
    keep None as absent data. The instruction was explicit — "don't include
    channels unless they have a specific location listed on YouTube" — and the
    measurement behind it is in the module docstring. This is now one of the
    two places (with the English-language gate) where the project deliberately
    breaks its own "absent data never disqualifies" rule.

    The three-state return is still worth keeping over a bool: the caller logs
    a DIFFERENT drop reason for "declared somewhere we don't serve"
    (`outside_search_zone`) than for "declared nothing at all"
    (`no_declared_country`), and a run summary that cannot tell those apart
    cannot tell a badly-targeted discovery query from a thin-metadata one.

    If a country genuinely outside the zones keeps slipping through as
    unknown, add its name to KNOWN_OUTSIDE_COUNTRY_NAMES — that table is what
    the title and description signals below match on, and it is still the
    right place to widen.
    """
    code = country_code(raw)
    if not code:
        return None
    return code in allowed_codes


# Regional indicator symbols (U+1F1E6-U+1F1FF) are just A-Z shifted, so a pair
# of them IS an ISO 3166-1 alpha-2 code — the Japanese flag is literally "JP".
# That makes a flag an EXACT location signal rather than a heuristic one,
# which is why it outranks every other signal in this module.
_FLAG_PAIR = re.compile("[\U0001F1E6-\U0001F1FF]{2}")
_REGIONAL_INDICATOR_BASE = 0x1F1E6


def flag_emoji_countries(text: str | None) -> set[str]:
    """Every ISO alpha-2 code spelled by a flag emoji in `text`."""
    return {
        "".join(chr(ord(c) - _REGIONAL_INDICATOR_BASE + ord("A")) for c in match.group(0))
        for match in _FLAG_PAIR.finditer(text or "")
    }


def flag_country_outside_zone(title: str | None, allowed_codes) -> str:
    """
    The code of an out-of-zone country whose FLAG the channel flies in its
    TITLE, or "". Caught the live row "Daichi" + Japanese flag, which declares
    `country: US` and posts Japanese travel content.

    **TITLE only, deliberately.** A flag in the About TEXT is usually a trip
    list on a travel channel, which would be a false positive in a lifestyle
    niche that legitimately contains travel creators. A flag in the channel's
    own NAME is an identity claim. Measured over 144 tracked rows exactly two
    carry a flag anywhere: the Daichi title (JP, correctly dropped) and
    `Vroon & Britt TV` (About text, Canadian flag, in zone anyway). Title-only
    therefore costs nothing today and closes the travel-list risk before it
    can open.

    Sorted so a title flying several out-of-zone flags reports a stable one
    rather than whichever the set happened to yield first.
    """
    outside = sorted(code for code in flag_emoji_countries(title) if code not in allowed_codes)
    return outside[0] if outside else ""


# Cues a creator uses to state where they actually LIVE, so a non-zone
# country right after one is their location rather than a passing mention.
#
# REPAIRED AND WIDENED 2026-08-20. The previous cue set fired on **0 of the
# 144 tracked rows** — it was inert on live data, and every channel it existed
# to catch was sitting in the table. Two faults, both measured:
#
#   1. "lives in" and "living in" were present but plain "live in" was NOT, so
#      first-person "I live in Belarus" (`Olesya & house`) and "We live in ...
#      Thailand" (`Thai Girl Gift & Foreigner Joe`) — the single most common
#      phrasing a creator uses — never matched. `lives?\s+in` now covers both.
#   2. The gap allowed between cue and country name was `[^\n,.]{0,30}`: 30
#      characters AND no comma or period. "We live in Mueang Prachuap Khiri
#      Khan Thailand" is 35 characters, and "from a village in Kenya Africa"
#      crosses a comma. It is now `[^\n]{0,60}` — still one line, still
#      bounded, but wide enough for a real address-shaped phrase.
#
# "from" is now a cue ONLY in the shape `from <up to two words> in <country>`
# ("from a village in Kenya"). Bare "from <country>" stays excluded for the
# original reason: "gear from Japan" and "clips from India" are passing
# mentions, and "parents from India" is not a location at all. "shot in" /
# "filmed in" stay out too — they describe where a video was made.
#
# Measured after the change over all 144 tracked rows: fires on 5, and all 5
# are true positives (CN, KE x2, BY, TH) with zero false positives. The
# rejected wider variant — any country name anywhere in the About text with no
# cue at all — fires on 11 and is plainly wrong on at least two.
# Every cue that ends in a word ("in", "to", "base") is anchored with \b.
# Without it the cue glues onto the FRONT of the next word and eats the
# country: "Clips from India and Pakistan" matched cue "from In", left "dia
# and Pakistan", and reported PK — a false positive on a bare mention, which
# is the exact failure this gate's cue requirement exists to prevent. "moved
# to" + "moved together" is the same bug. 📍 and the `location:` form end in
# punctuation and take no anchor.
_LOCATION_CUE = (
    r"(?:based in\b|based out of\b|located in\b|living in\b|lives?\s+in\b|"
    r"moved to\b|home (?:is )?in\b|home base\b|life in\b|"
    r"from(?: a| the)?(?: \w+){0,2} in\b|📍|location\s*[:\-])"
)
# How far past the cue a country name may sit. See fault 2 above.
_LOCATION_WINDOW = 60
# Longest names first so "south korea" wins over "korea", "hong kong" over a
# stray "kong", etc.
_OUTSIDE_NAME_ALTERNATION = "|".join(
    re.escape(n) for n in sorted(KNOWN_OUTSIDE_COUNTRY_NAMES, key=len, reverse=True)
)
_OUTSIDE_NAME_PATTERN = re.compile(
    r"\b(" + _OUTSIDE_NAME_ALTERNATION + r")\b", re.IGNORECASE
)
_DESC_LOCATION_PATTERN = re.compile(
    _LOCATION_CUE + r"[^\n]{0," + str(_LOCATION_WINDOW) + r"}?\b("
    + _OUTSIDE_NAME_ALTERNATION + r")\b",
    re.IGNORECASE,
)


def _outside_code(match) -> str:
    # The alternation is built from already-normalized (lowercase) keys with
    # the \b anchors OUTSIDE the capture group, so the group cannot carry
    # surrounding whitespace — only case differs, the pattern being
    # IGNORECASE — which makes .lower() enough to hit the (lowercase)
    # KNOWN_OUTSIDE_COUNTRY_NAMES key.
    return KNOWN_OUTSIDE_COUNTRY_NAMES[match.group(1).lower()]


def title_country_outside_zone(title: str | None) -> str:
    """
    The ISO code of an out-of-zone country NAMED IN THE CHANNEL TITLE, or "".

    Added 2026-08-20 for two live rows that declare an in-zone country and say
    where they are in their own name: `LIV KENYA` (declares GB) and
    `Inside Japan Living` (declares US). A creator who puts a country in the
    channel NAME is not mentioning it in passing.

    Scoped to `KNOWN_OUTSIDE_COUNTRY_NAMES`, i.e. the far-outside set, and so
    it takes no `allowed_codes`: it can only ever vote "outside", never
    "inside", and a European name is not in that table at all — a German
    channel is caught by the declared-country check instead. Keeping it narrow
    is what keeps it precise.

    Word-boundary matching does useful work here for free: "Japanese Joinery"
    does NOT match "japan", because the trailing \\b requires a non-word
    character next. Measured over all 144 tracked titles it fires exactly
    twice, both true positives, zero false positives.
    """
    match = _OUTSIDE_NAME_PATTERN.search(title or "")
    return _outside_code(match) if match else ""


def description_location_outside_zone(description: str | None) -> str:
    """
    The ISO code of a non-zone country a channel's About description gives as
    its LOCATION (e.g. "based in the Philippines" -> "PH"), or "".

    Catches creators who set snippet.country to an IN-ZONE country but reveal
    their real, outside-the-zone location in their description — which,
    measured over the live tables, is the COMMON case rather than the exotic
    one: nine of the twelve genuinely out-of-zone rows declare US, GB or CA.

    An explicit location cue is required before the country name, so a passing
    mention ("gear from Japan", "shot in Iceland") does not trip it, and only
    OUTSIDE names are matched, so an in-zone location ("based in Canada")
    never fires. It is still a heuristic: a location given only as a city
    ("straight from Guangzhou") or as a flag inside the About text is missed,
    but the burden is on excluding, so a miss just means a human might review
    one. See the _LOCATION_CUE comment for what was repaired and measured.
    """
    match = _DESC_LOCATION_PATTERN.search(description or "")
    return _outside_code(match) if match else ""
