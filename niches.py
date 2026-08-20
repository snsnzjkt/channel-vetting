"""
The niche registry — what gets searched, and which Airtable table it lands in.

Extracted from `main.py` (2026-08-14) so `outreach.py` can read the niche ->
table mapping without importing the pipeline. `import main` executes this dict
AND pulls in discovery, enrichment, influencers and browser_email — i.e.
Playwright — into a process whose only job is sending an email.

`main.py` re-exports NICHES from here, so this stays the single source of
truth: adding a niche in one place is still enough.
"""
import logging

from config import AIRTABLE_TABLE_HOME_THEATER, AIRTABLE_TABLE_LIFESTYLE_SOFA
from search_zones import ZONE_CORE, vendor_locations_for

logger = logging.getLogger(__name__)

# One entry per niche: its search keywords (drawn directly from the
# "Types of Content Posting" > Primary section of each influencer
# profiling brief, Cynthia Lim, updated 15 April 2024 — i.e. actual video
# topics target creators publish, not demographic/psychographic traits,
# those aren't searchable YouTube content) and which Airtable table its
# discovered channels get pushed to. Re-tune keywords as new briefs come
# in or results drift off-niche.
NICHES = {
    "Home Theater": {
        "keywords": [
            "home theater products review",
            "man cave tour",
            "entertainment room makeover",
            "car and truck review",
            "power tools review",
            "sports podcast commentary",
            "movie review and reaction",
            "home theater tech setup",
            "homesteading vlog",
        ],
        "table_name": AIRTABLE_TABLE_HOME_THEATER,
        # Which of the base's EXTERNAL outreach tables belong to this niche,
        # matched case-insensitively as a substring of the source table name in
        # external_dedupe.EXTERNAL_TABLES. Spelled "Theatre" because that is how
        # those tables spell it — the niche key is "Theater", which is exactly
        # why this is an explicit field and not derived from the key.
        #
        # Only used to PRIORITISE the discovery exclude set under the vendor's
        # 10,000-handle cap (see _external_priority); it never changes which
        # candidates are deduped, only which exclusions fit in the request.
        "external_source_hint": "Home Theatre",
        # From the Home Theater brief (Cynthia Lim, 15 April 2024):
        # "Has a Min 10k+ views on YouTube" and "Not a new channel".
        # The 10,000 figure is now the floor BOTH niches run on, so this
        # entry is unchanged by the 2026-08 criteria change — it is the one
        # the other niche moved to. The threshold stays per-niche rather
        # than becoming a shared constant so a niche can be given its own
        # bar again without unpicking the gate.
        "min_avg_views": 10_000,
        "min_channel_age_months": 12,
        # NARROWED 2026-08-20 from US/CA/UK/Europe/AU to US/CA/UK/AU. The
        # instruction named Lifestyle ("Europe is not in our search zone for
        # lifestyle. Only UK USA CANADA and AUS") and the standing instruction
        # to keep the two niches' criteria unified carries it here — the same
        # reasoning that moved the other niche's view floor to match this one.
        # Cost, measured over this table's 60 tracked rows: 5 European rows
        # (2 DE, NO, FR, CH). To put Europe back for this niche alone, write
        # `ZONE_CORE | EUROPE_COUNTRY_CODES` — search_zones still defines it.
        #
        # Per-niche rather than a module constant for the same reason
        # min_avg_views is: these two niches' criteria have already diverged
        # and reconverged once, and a shared constant would have to be
        # unpicked from the gate to let them diverge again.
        "allowed_country_codes": ZONE_CORE,
        # influencers.club discovery filters (the source that replaces
        # search.list when INFLUENCERS_API_KEY is set — see run_niche). The
        # products being promoted are home-theatre gear, so the creators worth
        # reaching are: theatre enthusiasts (home cinema / AV / media rooms),
        # homebodies (people who build their nights-in around home
        # entertainment), and furniture enthusiasts (media-/living-room
        # furnishing). Relevance is carried by the ai_search SEMANTIC query,
        # not by `topics`: the yt-topics taxonomy has no leaf for "home" or
        # "furniture", and pinning topics to Movies/Technology would EXCLUDE
        # the furniture/homebody creators (YouTube files them under Lifestyle).
        # Reword ai_search to steer the niche; it is a 3–150 char free-text
        # field verified live 2026-08-13.
        #
        # gender="male": the primary target for home-theatre products is men.
        # This is the CREATOR's gender, filtered server-side (accepted values
        # verified live: 'any' | 'male' | 'female'). There is also a separate
        # audience.gender filter (target creators whose AUDIENCE skews male) if
        # audience composition ever matters more than the creator's own gender.
        "discovery_filters": {
            "profile_language": ["en"],
            "gender": "male",
            # WIDENED 2026-08-14 after measuring the addressable pool. The old
            # query ("home theater and home cinema setups, media rooms, cozy
            # homebody home entertainment, living room furniture and home
            # furnishing") matched only 444 creators against Lifestyle Sofa's
            # 7,647 — a 17x gap, and the reason this table stopped producing new
            # prospects while the other kept filling. A fixed pool that small is
            # consumed in a few runs, after which exclude_handles leaves almost
            # nothing.
            #
            # Probed one filter at a time (limit=1, so 0.01 credits each) to find
            # what was actually binding. Results, all with gender=male and
            # subs>=5000 held constant:
            #
            #   current wording ......................  444
            #   + projectors / AV receivers / soundbars  445   <- no effect
            #   + man cave / gaming setup / home audio  2623   <- 5.9x
            #   broad "home entertainment" wording      1743
            #
            # The technical AV vocabulary buys nothing; the LIFESTYLE framing is
            # what opens the pool, because home-theatre buyers overlap heavily
            # with man-cave and gaming-setup creators. Gender and the subscriber
            # floor were the other candidates and are much weaker levers
            # (dropping gender entirely: 444 -> 1572; subs 5000 -> 2000: 444 ->
            # 635), so neither was touched — the male-creator preference stands.
            #
            # KEEP THIS UNDER 150 CHARACTERS. The vendor documents 3-150, and a
            # 180-char version measured WORSE (1,039) than this 122-char one,
            # which reads like silent truncation. Re-probe with the snippet above
            # after any reword; do not assume more terms means a wider pool.
            "ai_search": (
                "home theater and home cinema, media room, man cave, gaming setup, "
                "home audio, projector and TV setup, living room furniture"
            ),
            # `number_of_subscribers` is wired in below the NICHES dict, derived
            # from this niche's own min_avg_views via
            # DISCOVERY_SUBSCRIBER_FLOOR_RATIO — see that constant.
            #
            # A server-side "keywords_not_in_description" negation of the
            # off-brand political / ASMR / firearms terms is wired in from
            # EXCLUDED_TOPIC_TERMS after that dict is defined (it lives below
            # this literal) — see EXCLUDED_TOPIC_KEYWORDS.
        },
    },
    "Lifestyle Sofa": {
        "keywords": [
            "interior design and styling",
            "home decor tour",
            "DIY home makeover",
            "day in the life stay at home mom",
            "home cleaning and organizing",
            "furniture review unboxing",
            "cozy living room decor",
            "country living home",
            "minimalist home living",
            "house tour apartment tour",
            "seasonal home decor",
        ],
        "table_name": AIRTABLE_TABLE_LIFESTYLE_SOFA,
        # See the Home Theater entry for what this does. "Lifestyle" matches
        # "Lifestyle – Sofa Influencers" (1,949 handles), which fits under the
        # cap with room to spare — so this niche's external re-bills go to zero.
        "external_source_hint": "Lifestyle",
        # RAISED from the brief's 2,000 to 10,000 in the 2026-08 criteria
        # change, which put both niches on the same view floor. The brief
        # (Cynthia Lim, 15 April 2024) says "Has min of 2k+ view on YouTube
        # videos" — this deliberately overrides it, so don't "restore" the
        # 2,000 from the brief without checking that the instruction to
        # unify the two niches has actually been reversed.
        #
        # The brief sets no channel-age requirement, and that part still
        # stands. Its Instagram thresholds (100k+ followers, 20k+ reel
        # views) are out of scope — this pipeline only observes YouTube.
        "min_avg_views": 10_000,
        "min_channel_age_months": None,
        # NARROWED 2026-08-20. This is the niche the instruction actually
        # named: "Europe is not in our search zone for lifestyle. Only UK USA
        # CANADA and AUS". Cost, measured over this table's 84 tracked rows:
        # 4 European rows (2 DE, FR, UA). See the Home Theater entry for why
        # this is a per-niche key and how to restore Europe.
        "allowed_country_codes": ZONE_CORE,
        # Fashion, lifestyle, travel, house tours, and home decor — and
        # especially women-led channels. gender="female" filters the CREATOR
        # server-side (values verified live: 'any' | 'male' | 'female').
        # Relevance rides on ai_search rather than topics: "house tours" and
        # "home decor" have no yt-topics leaf, and pinning topics to the
        # Fashion/Tourism leaves that DO exist would exclude the decor /
        # house-tour creators. Reword ai_search to steer it.
        "discovery_filters": {
            "profile_language": ["en"],
            "gender": "female",
            "ai_search": "fashion and lifestyle vlogs, travel, house tours, home decor and interior styling",
            # As with Home Theater, `number_of_subscribers` is derived from this
            # niche's own min_avg_views below the dict — so if this niche's view
            # floor is ever un-unified back toward the brief's 2,000, its
            # subscriber floor follows automatically instead of going stale.
            #
            # As above: the off-brand "keywords_not_in_description" negation is
            # wired in from EXCLUDED_TOPIC_TERMS below — see
            # EXCLUDED_TOPIC_KEYWORDS.
        },
    },
}


# ---------------------------------------------------------------------------
# Everything below moved here from main.py (2026-08-14) because
# wire_discovery_filters() MUTATES the NICHES dict above, in place, at import.
# While it lived in main.py, `import niches` on its own returned a registry
# with no `keywords_not_in_description` and no `number_of_subscribers` — the
# off-brand-topic exclusions and the subscriber floor — so the module was only
# correct if some other module had been imported first. That is exactly the
# import-order spooky action this repo is otherwise careful about, and the
# consequence was not cosmetic: an unfiltered discovery query re-buys the
# off-niche creators the topic gate exists to keep out, at 0.01 credits each.
#
# main.py imports EXCLUDED_TOPIC_TERMS back from here for its local
# excluded_topic_reason() backstop, which is the correct direction: the data
# describes what we are and are not looking for, which is this module's job.
# ---------------------------------------------------------------------------

# The subscriber floor sent to influencers.club's SERVER-SIDE
# `number_of_subscribers` filter, expressed as a FRACTION of the niche's own
# `min_avg_views` rather than an absolute number. Raised from a flat 2,000 on
# 2026-08-14 and wired per-niche below the NICHES dict.
#
# This is the one legitimate use of a vendor statistic in the pipeline: it
# decides which creators we PAY to look at, never what we believe about a
# channel. Every number that gates, scores, or gets written still comes from the
# YouTube Data API v3 (see influencer_discovery._to_candidate).
#
# Why 2,000 was wrong: discovery bills 0.01 per creator returned, and a channel
# has to clear its niche's average AND have 70% of its recent videos over
# MIN_VIEWS_PER_VIDEO to become a row. Against a 10,000 average that made 2,000
# subscribers a 5x view-to-subscriber ratio — rare enough that the old floor
# screened almost nothing out. We paid for those creators and then discarded
# them at the view gate.
#
# Why a RATIO and not an absolute: every other qualification lever here
# (min_avg_views, min_channel_age_months) is per-niche, because the two niches'
# thresholds have already diverged and reconverged once (Lifestyle Sofa's view
# floor was 2,000 before the unification). An absolute subscriber floor would
# silently stop matching the arithmetic that justified it the moment a niche's
# view floor moved, and no test would catch the drift. Deriving it means the
# floor tracks its own niche automatically.
#
# Why 1.0, RAISED FROM 0.5 on 2026-08-20 against measured drop-reason counts —
# which is what the previous note here asked for ("tune against the drop-reason
# counts in a run summary, not by intuition"). The asymmetry it described still
# holds: too high is a FALSE NEGATIVE, a real prospect the vendor never shows us,
# invisible and unrecoverable; too low only costs credits. The measurement is
# what moved, and it showed the old floor screening out almost nothing.
#
# What forced it: a 40-creator sample of fresh Home Theater discovery, run
# through the real gates, produced ZERO rows, and 45% of it died on
# `below_view_minimum`. At 0.5 the floor was 5,000 subscribers against a 10,000
# average-view requirement — a 2x ratio, which is ordinary, so nearly every
# under-performing channel in the niche cleared it and was billed for.
#
# Why 1.0 and not higher, calibrated on the rows that ACTUALLY QUALIFIED (60
# Home Theater, 84 Lifestyle Sofa) rather than on the ratio argument alone:
#
#     floor    HT qualifiers kept   LS qualifiers kept   HT pool
#      5,000          98%                 100%            2,619
#     10,000          95%                  99%            1,985   <- this
#     20,000          88%                  94%            1,425
#     30,000          72%                  92%            1,158
#     50,000          63%                  76%              864
#
# 10,000 is the last floor that costs almost no reachable prospect (95% / 99%
# retained) while removing a quarter of the pool. 20,000 removes 46% of the pool
# but gives up 12% of real Home Theater qualifiers, and false negatives are the
# unrecoverable direction — so the pool reduction beyond this point is not worth
# buying. Measured medians for context: avg_views/subscribers is 0.405 (Home
# Theater) and 0.218 (Lifestyle Sofa), i.e. a typical qualifier has FAR more
# subscribers than its view floor, which is why this floor can rise this far
# without cutting into the real population.
#
# NOTE the view floor itself cannot be filtered server-side: the vendor accepts
# an `average_views` filter and silently ignores it (verified — total unchanged
# at min=100,000,000). The subscriber floor is the only lever the vendor honours,
# which is why it carries this much weight.
DISCOVERY_SUBSCRIBER_FLOOR_RATIO = 1.0


# Whole categories a brand-partnership run must never surface, however well a
# channel otherwise fits a niche: political commentary, ASMR, and firearms /
# gun-review content. Discarded outright like the gates above (not flagged) —
# the brief rules them out, so a human reviewing them is pure cost.
#
# Matched as WHOLE WORDS (case-insensitive) against the channel's OWN title
# and About description only — its self-identification — never its video
# descriptions, which would drag in false positives (a home-theater channel
# reviewing a war film; a decor channel styling a "campaign" desk).
#
# The term lists are deliberately tuned against THIS pipeline's two niches,
# where the obvious words are landmines:
#   - firearms omits bare "gun"/"shotgun"/"shooting" ("nail/glue/spray gun"
#     are DIY/furniture vocabulary; "shotgun" is a home-theater MICROPHONE),
#     AND omits "pistol"/"revolver"/"rifle": Home Theater is audiophile-
#     adjacent and Lifestyle Sofa covers fashion/thrift, so those collide
#     with the Sex Pistols, the Beatles' "Revolver", and the verb "rifle
#     through". The remaining firearm-specific terms still catch a gun-review
#     channel, which will carry firearm/handgun/ammo/AR-15/glock/etc.
#   - political omits "conservative"/"liberal" (everyday decor/design
#     adjectives: "a conservative palette", "liberal use of throw pillows")
#     and "parliament" (the funk band Parliament-Funkadelic).
# Over-excluding costs one lead; under-excluding lets a banned category
# through. Tune the sets if a real prospect is ever wrongly dropped.
EXCLUDED_TOPIC_TERMS = {
    "political": [
        "politics", "political", "geopolitics", "election", "elections",
        "democrat", "democrats", "republican", "republicans", "libertarian",
        "leftist", "left-wing", "right-wing", "congress", "senate",
        "communism", "socialism", "MAGA",
    ],
    "asmr": ["asmr", "tingles", "mouth sounds"],
    "firearms": [
        "firearm", "firearms", "handgun", "handguns", "ammo", "ammunition",
        "AR-15", "AK-47", "glock", "concealed carry", "second amendment",
        "gunsmith", "ballistics",
    ],
    # --- WRONG VERTICAL (2026-08-15) -------------------------------------
    # Added after two channels reached the Home Theater table that no gate
    # could have stopped, because no gate asks about RELEVANCE: fit is
    # delegated entirely to influencers.club's ai_search, which was widened
    # 5.9x on 2026-08-14 for pool size and never re-checked for precision.
    #
    # This is a BLOCKLIST, and it is honestly whack-a-mole — it catches the
    # verticals named here and nothing else. A general relevance gate was
    # built and MEASURED first, and rejected on the numbers:
    #
    #   - On BIOS: unusable. Four tracked channels have no vocabulary at all
    #     ("Hi!", "Collab: <email>", a bare email address), so a
    #     must-match-a-term rule discards real prospects on an empty bio.
    #   - On the 50 VIDEO TITLES (free — already fetched for the duplicate
    #     filter): better, but it does not separate the two groups. Measured
    #     over all 38 Home Theater rows, the racing channel scored 2/50 and
    #     WOULD have been caught, but the logging channel scored 25/50 (its
    #     titles carry "furniture", "home decor", "interior" from woodworking)
    #     and would NOT, while "Jasper Tran - House Design Ideas" scored 0/50
    #     and a real prospect would have been discarded. A threshold that
    #     catches one of the two reported channels costs two false positives
    #     and still misses the other.
    #
    # Both channels ARE caught cleanly by their own bios, which is what these
    # terms read. Terms are kept narrow on purpose — each one also goes to the
    # vendor as keywords_not_in_description, so a sloppy term silently shrinks
    # the discovery pool as well as dropping rows.
    "sim_racing": [
        # Game TITLES, not "racing": "car and truck review" is a deliberate
        # Home Theater keyword, and a creator who builds a sim rig in their
        # man cave is a legitimate prospect. What is off-niche is gameplay
        # content, and a gameplay channel names the games in its bio.
        "beamng", "assetto corsa", "iracing", "gran turismo",
    ],
    "forestry": [
        # "logging truck" and not bare "logging": the word-boundary match
        # already spares "vlogging", but "logging" alone would still fire on
        # "logging my progress". "timber"/"chainsaw" are deliberately OMITTED
        # — "timber furniture" is ordinary AU/UK furniture vocabulary for
        # Lifestyle Sofa, and "power tools review" is a Home Theater keyword.
        "logging truck", "logging trucks", "forestry", "sawmill",
        "tree felling",
    ],
}

# ---------------------------------------------------------------------------
# BROADCASTERS AND TV SHOWS (2026-08-20)
#
# Added after HGTV (1.07M subs), Entertainment Tonight (7.71M) and Escape To
# The Country — a BBC daytime property programme — were all written into the
# Lifestyle table as **Qualified**. Nothing in the pipeline asked whether a
# channel is a person or a broadcaster, so all three cleared every gate: they
# post long-form English video, well over the view floor, from an in-zone
# country. A brand-partnership run cannot use a television network.
#
# Kept SEPARATE from EXCLUDED_TOPIC_TERMS above rather than added as another
# category there, for two reasons that both come out of the measurement:
#
#   1. **The two lists need different SCOPES.** Network names are matched
#      against the channel TITLE ONLY. Matching them in the About text was
#      measured over all 144 tracked rows and produces false positives
#      immediately: `Drew & Jonathan` say "you probably know us from our HGTV
#      shows" and `Traveling with Kristin` lists BBC among her press credits.
#      Both are genuine creator channels. A creator MENTIONS a network; a
#      network IS one. EXCLUDED_TOPIC_TERMS is title+bio and must stay that
#      way, so these cannot share its matcher.
#   2. **These must NOT reach the vendor.** Every term in
#      EXCLUDED_TOPIC_TERMS is flattened into EXCLUDED_TOPIC_KEYWORDS and sent
#      as influencers.club's `keywords_not_in_description`, which is a BIO
#      negation — precisely the scope proved unsafe in (1). Sending "bbc"
#      would have withheld Traveling with Kristin from discovery entirely.
#      The credit saving forgone is trivial (0.01 per creator, and this fires
#      on 3 of 144 rows); the pool damage would not have been.
#
# Verified over all 144 tracked rows: exactly HGTV, Entertainment Tonight and
# Escape To The Country fire. Zero false positives. This is the same honest
# whack-a-mole as sim_racing / forestry above — it catches the broadcasters
# named here and the shows that describe themselves as shows, and nothing
# else. It deliberately does NOT catch corporate/brand channels that are not
# television (Dolby, ADAM Audio, Apartment Therapy are all still admitted);
# widening to those was considered and declined as a separate decision.
#
# Matched on the channel TITLE ONLY — see reason (1) above. Keep this list to
# broadcaster BRANDS whose name in a channel title can only mean the
# broadcaster. Show names do not belong here: they are unbounded, and
# BROADCAST_TV_PHRASE_TERMS catches a show generically instead (Escape To The
# Country is caught by "daytime television", not by its own name).
BROADCAST_TV_NAME_TERMS = [
    "hgtv", "entertainment tonight", "food network", "diy network",
    "magnolia network", "discovery channel", "travel channel",
    "history channel", "bbc", "itv", "channel 4", "channel 5", "sky news",
    "cnn", "msnbc", "fox news", "abc news", "nbc news", "cbs news", "pbs",
    "tlc", "bravo tv", "a&e", "lifetime tv", "paramount network",
    "nickelodeon", "disney channel",
]

# Matched on the channel title AND the About description. These are how a
# broadcast property describes ITSELF, and they generalise past the name list
# — "a British daytime television property-buying programme, first airing in
# 2002" is Escape To The Country's own bio, and no creator writes that.
#
# Every term is multi-word on purpose. Bare "television" was measured and
# rejected: `CritiX tv`, a film-and-TV fan-review channel and a legitimate
# creator, uses the word three times in its bio. "official youtube channel of"
# was also measured and dropped — it caught ADAM Audio, a speaker
# manufacturer, which is a brand channel but not a TV one, and this gate is
# scoped to TV.
BROADCAST_TV_PHRASE_TERMS = [
    "full episodes", "full episode", "new episodes air", "episodes air",
    "airs every", "first airing", "first aired", "originally aired",
    "television programme", "television program", "tv programme",
    "television series", "television network", "tv network",
    "broadcast network", "television channel", "daytime television",
    "television programming", "season premiere", "series premiere",
    "tune in every",
]


# The off-brand terms from EXCLUDED_TOPIC_TERMS ONLY — deliberately NOT the
# broadcast-TV lists above, which are local-only for the reasons recorded
# there — flattened for influencers.club discovery's
# SERVER-SIDE negation filter (see the wiring loop below). Sent as the
# vendor's `keywords_not_in_description`, which withholds any creator whose
# profile bio carries one of these words/phrases — so the whole political /
# ASMR / firearms categories are never RETURNED, and (at 0.01 credits per
# returned creator) never BILLED. This is the credit-saving move
# exclude_handles already makes for already-known creators: filtering these
# out locally after the response, the way excluded_topic_reason() does, cannot
# refund the discovery credit the vendor has already charged.
#
# Reuses EXCLUDED_TOPIC_TERMS verbatim rather than a hand-kept copy, so the
# server pre-filter and the local backstop can never drift. Safe to reuse
# because the vendor field matches the SAME way the local gate does — case-
# insensitive, on whole words and multi-word phrases (verified live
# 2026-08-14, the way gender's accepted values were: a wrong-type probe
# names the field, and keywords_in/keywords_not partition the result set
# exactly — P + N == base total). So the landmine words that gate deliberately
# omits ("gun"/"rifle"/"conservative"/…) stay omitted here too; "MAGA" does
# not match "magazine".
EXCLUDED_TOPIC_KEYWORDS = sorted(
    {term for terms in EXCLUDED_TOPIC_TERMS.values() for term in terms}
)

def wire_discovery_filters(niches: dict) -> None:
    """
    Fill in the server-side discovery filters that can't be written inline in
    the NICHES literal, because they derive from things defined after it.

    Called at import, immediately below. A named function rather than a
    top-level for-loop for two reasons: a test can hand it a deliberately
    misconfigured niche (a bare loop here could only be exercised by
    re-importing the module), and it needs no `del` of throwaway loop names
    afterwards.

    Mutates `niches` in place. Every lookup is guarded with `in` rather than
    indexed, and that is load-bearing rather than defensive noise: this runs
    while `import main` is still executing, so a KeyError here kills the run
    before logging is configured, before the blocklist fetch, before any niche
    is attempted. That is strictly worse than the failure this project
    deliberately designed for, where run_niche() checks the same keys against
    REQUIRED_NICHE_KEYS and skips only the offending niche with a logged error.
    Leaving a filter unset routes a misconfigured niche back to that check.
    """
    for niche_config in niches.values():
        filters = niche_config.get("discovery_filters")
        if filters is None:
            continue  # search.list-only niche; nothing to wire

        # A per-niche list() copy, not the shared constant itself: each niche
        # owns its own list, so a future per-niche exclusion edit can't mutate
        # the other niche's filter (or EXCLUDED_TOPIC_KEYWORDS) in place. The
        # copies are still all derived from EXCLUDED_TOPIC_TERMS at import, so
        # the no-drift guarantee above is unaffected.
        filters["keywords_not_in_description"] = list(EXCLUDED_TOPIC_KEYWORDS)

        # The subscriber floor, derived from THIS niche's own view floor so the
        # two can never drift apart — see DISCOVERY_SUBSCRIBER_FLOOR_RATIO.
        # int(), because the vendor's number_of_subscribers.min is an integer
        # field and a float would be a type error at the API rather than here.
        if "min_avg_views" in niche_config:
            filters["number_of_subscribers"] = {
                "min": int(niche_config["min_avg_views"] * DISCOVERY_SUBSCRIBER_FLOOR_RATIO)
            }

        # The SERVER-SIDE half of the search-zone gate, derived from the same
        # `allowed_country_codes` that location_drop_reason() enforces locally,
        # so the two can never disagree about which zone a niche runs on.
        #
        # Added 2026-08-20. Until now the zone existed only as a local gate: the
        # vendor was asked for creators anywhere on earth and 72% of what it
        # returned for Home Theater was out of zone (2,619 -> 734 once the filter
        # is applied; Lifestyle Sofa 7,647 -> 3,002). Every one of those was
        # billed at 0.01 and then discarded by location_drop_reason() or by
        # `no_declared_country` — in a measured 40-creator sample, 5% died on the
        # country check alone, and the out-of-zone share of the pool is far
        # larger than that because relevancy sorting front-loads the in-zone ones.
        #
        # vendor_locations_for() is ALL-OR-NOTHING: a zone containing a code with
        # no verified vendor spelling yields [] and the filter is skipped, which
        # is the old behaviour. See its docstring — sending a partial list would
        # exclude creators the niche allows, and an unverified name would 400 the
        # whole request. Skipping is the safe failure, so the warning is what
        # makes it visible rather than a silent narrowing.
        allowed_codes = niche_config.get("allowed_country_codes")
        if allowed_codes:
            locations = vendor_locations_for(allowed_codes)
            if locations:
                filters["location"] = locations
            else:
                logger.warning(
                    "Niche zone %s has no verified influencers.club location name for "
                    "every code, so the server-side `location` filter is OFF for it. "
                    "Out-of-zone creators will be returned and billed at 0.01 each "
                    "before location_drop_reason() discards them. Add the verified "
                    "spellings to search_zones.VENDOR_LOCATION_NAMES to switch it on.",
                    sorted(allowed_codes),
                )


# The local excluded_topic_reason() gate in process_candidate STAYS as the
# deterministic backstop to the keywords_not_in_description filter wired above:
# it also reads the channel TITLE (not just the bio), and it is the only tier
# that covers the search.list fallback path the server-side filter never sees.
wire_discovery_filters(NICHES)
