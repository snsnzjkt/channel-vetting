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

from config import AIRTABLE_TABLE_HOME_THEATER, AIRTABLE_TABLE_LIFESTYLE_SOFA, DISCOVERY_SUBSCRIBER_FLOOR_RATIO as CONFIG_DISCOVERY_SUBSCRIBER_FLOOR_RATIO
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
        # RESCUE vocabulary for main.off_target_reason — NEVER an admission
        # test. A channel is never kept because it matches these and never
        # dropped for failing to; they exist only so a channel the off-target
        # vocabulary flagged can survive when its content is genuinely on-niche.
        # That asymmetry is the whole design — see OFF_TARGET_TERMS for why the
        # positive-requirement version was measured and rejected. Measured:
        # "OCM Reviews" (DACs, IEMs, Atmos soundbars) scores 0.06 off / 0.60 on
        # and is rescued; "DragsterTV" (Forza money glitches) scores 0.04 off /
        # 0.00 on and is dropped. No single signal separates those two.
        # WIDENED 2026-08-22 toward ROOM and FURNITURE vocabulary, on reviewer
        # instruction: accept a channel that "did a room tour or mostly home
        # furniture type of content", even when not all of its content is that.
        #
        # The old list was pure AV EQUIPMENT — projector, soundbar, atmos, DAC,
        # IEM. That is a narrower reading of the niche than the reviewer's, and
        # the earlier backtest said so out loud: across 96 labelled rows an
        # equipment-focus score was INVERTED against the verdict (27% approved
        # vs a 38% base rate; the five most equipment-focused channels were all
        # rejected). The reviewer is buying an AUDIENCE for home-entertainment
        # FURNITURE, not gear-review expertise.
        #
        # These terms only ever RESCUE — see main.off_target_reason. Adding one
        # can never admit a channel on its own; it can only raise on_share so
        # that a partly-off-topic channel is not dropped for the off-topic part.
        # That is exactly the "not all content is like that" case.
        "on_target_terms": [
            "room tour", "house tour", "home tour", "apartment tour",
            "setup tour", "setup reveal",
            "basement tour", "basement finish", "room makeover", "room reveal",
            "room setup", "entertainment center", "entertainment room",
            "tv stand", "tv wall", "tv mount", "media console", "media cabinet",
            "sectional", "sofa", "couch", "seating", "furniture",
            "living room", "family room", "game room", "bonus room",
            "home renovation", "home improvement", "interior",
            "home theater", "home theatre", "home cinema", "projector",
            "projection screen", "projector screen", "soundbar", "atmos",
            "surround", "av receiver", "media room", "man cave", "dolby",
            "5.1", "7.1", "sonos", "theater seating", "recliner", "4k hdr",
            # REMOVED 2026-08-22: speaker, subwoofer, amplifier, hi-fi, hifi,
            # audiophile, dac, iem, headphone, turntable, vinyl, klipsch,
            # denon, marantz, bookshelf speaker, acoustic, listening room,
            # home audio.
            #
            # Measured as RESCUE terms over 21 approved / 31 rejected rows they
            # saved 0 approved channels and 6 rejected ones — working
            # exclusively for the channels the reviewer turns down. "speakers"
            # appears in 0 of 21 approved and 8 of 31 rejected titles.
            # They are an EXCLUSION now: OFF_TARGET_TERMS["av_specialist"].
            # Both halves are required, because a term on both lists scores
            # off == on and the gate needs off > on to fire.
        ],
        # GEMINI RESCUE CRITERIA. Read only when main's title-based
        # off_target_reason has FLAGGED a candidate, and used only to overturn
        # that flag — never to drop a candidate the keyword gate let through.
        # See gemini_verify.GeminiVerifier.judge for why that asymmetry is the
        # whole safety argument.
        #
        # Two rules for editing these:
        #   1. A VIDEO criterion must be answerable from ~25 seconds of footage
        #      alone. "Does the creator own their home" is not; "is a person
        #      presenting to camera" is.
        #   2. Editing either list invalidates every cached verdict for this
        #      niche automatically (the criteria are hashed into the cache key),
        #      so retuning costs requests, never correctness.
        # Keep to 2-4 entries per list: each one is a separate judgement the
        # model has to evidence, and a long list dilutes all of them.
        "text_criteria": [
            {"name": "home AV / entertainment-space focus",
             "test": "Across these titles and descriptions, is the channel's "
                     "recurring subject home audio-visual equipment or the "
                     "entertainment spaces built around it — speakers, "
                     "projectors, receivers, soundbars, media rooms, man caves — "
                     "rather than general consumer tech, phones, PCs, or gaming?"},
            {"name": "reviews or builds, not news",
             "test": "Does the channel actually review, install or build this "
                     "equipment, rather than reporting industry news, reacting to "
                     "other creators, or reselling manufacturer announcements?"},
        ],
        # LOOSENED 2026-08-21, and deliberately. The previous three criteria
        # required that AV equipment be the video's OWN subject and that the
        # creator be presenting to camera. The backtest measured that as
        # INVERTED against the reviewer's verdict: the five channels scoring
        # highest were all Rejected, while Approved channels scored a median of
        # 10 (see GEMINI_VERIFY_PLAN.md 2.16). The reviewer is evidently buying
        # audience fit for home-entertainment FURNISHINGS, not equipment-review
        # focus, so "the equipment is the subject" was the wrong bar.
        #
        # Two criteria instead of three, each widened:
        #   - the SPACE now counts as much as the equipment, and incidental is
        #     fine so long as a real home living space is on screen;
        #   - voiceover over the creator's own footage counts, which is how a
        #     large share of real creators actually shoot;
        #   - the "not gaming / not generic gadgets" test is GONE. It duplicated
        #     what off_target_reason already does on keywords, so a channel was
        #     being penalised twice for one thing.
        "video_criteria": [
            {"name": "home entertainment or living space",
             "test": "Does this clip show, discuss, build, review or tour a home "
                     "entertainment or living space, or the equipment, seating or "
                     "furnishings in one? A room tour, a renovation, a build, a "
                     "product review or a setup walkthrough all count. The "
                     "equipment does not have to be the main subject: a real home "
                     "living space on screen is enough."},
            {"name": "a real creator, not a repost",
             "test": "Is there a real person on camera, OR a voice narrating "
                     "footage they appear to have shot themselves? Answer no only "
                     "if this looks like reposted manufacturer material, stock "
                     "footage, or a slideshow of stills with no creator present."},
            {"name": "an independent creator, not a brand",
             # REQUIRED: a veto, not a scored criterion. The ratio route above is
             # meant to loosen how much CONTENT relevance is demanded, and a
             # manufacturer or publisher is not two-thirds eligible. Measured:
             # ADAM Audio was correctly caught here and then re-admitted at 2/3
             # before this flag existed.
             "required": True,
             "test": "Is this an individual creator's own channel, rather than a "
                     "company, retailer, publisher, manufacturer, studio or TV "
                     "brand posting produced marketing content? Signs of a brand: "
                     "polished agency-style production with no identifiable host, "
                     "a presenter speaking on behalf of a company, product B-roll "
                     "with voiceover and no personality, or a logo bug throughout. "
                     "A single person filming in their own home or workshop is an "
                     "independent creator even when the production is good."},
        ],
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
        # SET 2026-08-22 after measuring the NET addressable pool, which is the
        # number every previous attempt at this niche was missing.
        #
        #   gross pool (query only) ........ 334
        #   cached rejects ................. 262
        #   NET, rejects excluded .......... 279   <- ~2 rows at the observed rate
        #
        # Every prior measurement here was GROSS: measure_discovery_pool's total()
        # sent `filters` and never `exclude_handles`, so it answered "creators
        # matching this query", never "creators we can still buy". Run it with
        # --net for the honest figure.
        #
        # 279 buyable creators is not a strictness problem and no gate change
        # reaches it. The `keywords` list above costs zero vendor credits, indexes
        # YouTube directly rather than the vendor's creator DB, and is not
        # exhaustible in the same way. It converts worse per candidate — the
        # vendor's server-side filtering is real value — but this niche has run
        # out of vendor corpus to convert.
        #
        # `discovery_filters` is deliberately KEPT, not deleted: it is what
        # measure_discovery_pool.py probes, and flipping this back to
        # "influencers" must stay a one-word change once the pool recovers (the
        # reject cache ages out at REJECTED_HANDLES_RETENTION_DAYS = 90).
        # MEASURED 2026-08-22 against the reviewer's own verdicts, and this is
        # the single biggest yield finding in the repo.
        #
        # off_target_reason was dropping 14 of the 21 Home Theater channels the
        # reviewer APPROVED (67%), while catching only 9 of 31 he rejected
        # (29%). Discrimination -38%: more than twice as likely to kill a good
        # channel as a bad one. Every category was individually harmful:
        #
        #   category           kills APPROVED   catches REJECTED
        #   gaming                48%               16%
        #   phones_and_pcs        52%               19%
        #   generic_gadgets       43%               13%
        #   ai_and_crypto         19%                6%
        #
        # The gate's own docstring named Bane Tech, DanKamYouKnow, Paul Antill
        # and NFT TIGERS as "hand-verified off-target". The reviewer approved
        # ALL FOUR. The calibration was verified against an engineer's idea of
        # on-niche, never against the labels — the same inversion the 96-row
        # relevance backtest found in 2026-08-21.
        #
        # What the labels actually say: for Home Theater the reviewer buys
        # general tech / gadget / setup creators (an AUDIENCE for home
        # entertainment furniture) and rejects the dedicated AV specialists
        # (Zero Fidelity, New Record Day, Forever Analog), the manufacturer
        # accounts (ADAM Audio, Dolby) and the media brands (HGTV, Apartment
        # Therapy, Drew & Jonathan). Excluding "gaming, phones/PCs, gadgets"
        # from this niche excludes precisely the approved profile.
        #
        # So this niche keeps ONLY the toys_and_kids category, which exists on
        # explicit reviewer instruction ("remove channels related to Lego",
        # "we cannot accept channels like this for HT"). Re-enable a category
        # here only with a backtest that shows it catches more rejections than
        # approvals.
        # Each entry measured against the reviewer's own verdicts (approved /
        # rejected counts in the OFF_TARGET_TERMS comments). The four omitted
        # categories — gaming, phones_and_pcs, generic_gadgets, ai_and_crypto —
        # were each more likely to kill an approved channel than catch a
        # rejected one. Add nothing here without a fresh backtest.
        "off_target_categories": [
            "toys_and_kids", "story_recap", "av_specialist",
            # Added after the first 90-day sweep. Each measured at 0 approved
            # channels killed across both niches; see OFF_TARGET_TERMS.
            "automotive", "movie_review_farm", "kids_craft",
            # sports_commentary was TRIED AND REMOVED 2026-08-22. Re-scored
            # against the refreshed labels (31 approved / 61 rejected, up from
            # 21/31) it killed 3 approved channels — JTL SPORTS, MAH, Cowboys
            # Report by Chat Sports — to catch 1 rejected. The reviewer also
            # approved "The Joel Klatt Show: A College Football Podcast".
            #
            # Sports commentary is NOT disqualifying for this niche, which also
            # validates the "sports podcast commentary" KEYWORD. The audience
            # theory holds: a man-cave sports audience is a home-entertainment
            # furniture audience.
        ],
        "discovery_source": "search_list",
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
            # REWRITTEN 2026-08-21. The previous wording contained the literal
            # phrase "gaming setup" and so ASKED THE VENDOR FOR GAMING
            # CHANNELS. It was added on 2026-08-14 for pool size (444 -> 2,623)
            # and never re-checked for precision: measured 2026-08-21, that one
            # phrase was carrying 370 of the niche's 588 creators, and 45% of
            # the rows it produced were gaming or generic-tech channels.
            #
            # Man caves, media rooms and home audio STAY — those are the
            # persona. What is gone is the word that matches a Fortnite channel.
            #
            # Re-probed at limit=1 (0.01 credits) per the rule below, because
            # more terms does not mean a wider pool. Measured totals:
            #   with "gaming setup" (the old wording) ......... 588
            #   this wording ................................. 209
            #   + "DIY home improvement and renovation" ...... 250
            #   + "movie room, basement media room" ..........  94
            #   + "entertainment room / setup" ............... 125
            #   AV-forward rewrite ...........................  95
            #
            # THE 250 VARIANT WAS TRIED FIRST AND REVERTED, and the reason is
            # the whole lesson of this field. Picking it meant picking the
            # biggest pool — which is precisely the reasoning that added
            # "gaming setup" on 2026-08-14 and caused this problem. Comparing
            # the top 20 BY RELEVANCY rather than the totals settled it:
            #
            #   with "DIY home improvement and renovation" (250): Under
            #     Construction with Tate, GrantMaury Builds, Sanborn
            #     Construction Group, Aspen Custom Carpentry — builders,
            #     roofers and a plumber. An allowed adjacency, but it had
            #     displaced the core persona at the top of the ranking, which
            #     is the only part of the ranking a run ever reaches.
            #   this wording (209): Audio Arkitekts, Linsoul Audio, Jay's Audio
            #     Lab, Pursuit Perfect System, SoundStage! Network, Sydney HiFi,
            #     Andrew Robinson — AV and hi-fi channels.
            #
            # 41 fewer creators for a materially better pool. JUDGE A REWORD ON
            # WHO IT RETURNS, NOT ON HOW MANY: a limit=20 probe costs 0.2
            # credits and answers the question the total cannot. The brief: "I
            # would rather have 20 highly relevant creators than 200 irrelevant
            # gaming/tech creators."
            "ai_search": (
                "home theater and home cinema, media room, man cave, home audio, "
                "projector and TV setup, living room furniture"
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
        # "both" = paid discovery FIRST, then top up from `keywords` in the same
        # run. Set 2026-08-22 because this niche is BUDGET-bound, not
        # pool-bound: measured 2,814 creators still buyable and only ~600
        # affordable per run at 6 credits, so 79% of its own pool goes
        # unexamined every run and more budget was declined.
        #
        # The free keyword corpus costs YouTube quota instead of credits, and
        # the keyword loop only spends the headroom paid discovery left, so this
        # adds output without adding spend. Paid stays FIRST because its
        # server-side filtering converts better per candidate — the free pass is
        # a top-up, not a replacement.
        "discovery_source": "both",
        # Stated EXPLICITLY rather than left to default-all, so adding a
        # category to OFF_TARGET_TERMS never silently changes this niche.
        # av_specialist is deliberately absent: it is measured for Home Theater
        # and untested here, and unmeasured strictness is what this whole
        # exercise has been undoing.
        #
        # property_showcase and travel_vlog each catch 2 rejected and kill 0
        # approved over 37/53 labelled rows. The first four are inert here
        # (0/37 and 0/53) and are kept only so the default is unchanged.
        "off_target_categories": [
            "gaming", "phones_and_pcs", "generic_gadgets", "ai_and_crypto",
            "toys_and_kids", "story_recap", "travel_vlog",
            # property_showcase was TRIED AND REMOVED 2026-08-22. On the
            # refreshed labels it caught 3 rejected but killed 1 approved —
            # Diana Oachis, whose titles are "Inside Oakville's $5.98 MILLION
            # MANSION" and "Madeira LUXURY Home Tour". The reviewer approves
            # some luxury home tours and rejects others, so the category does
            # not separate what he wants and a net +2 is not worth a lost
            # prospect when the brief is "still want many output".
            # 123 GO!-style kids craft/prank content is not a furniture prospect
            # either. Measured 0 of 37 approved Lifestyle channels affected.
            "kids_craft",
        ],
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
        # RESCUE vocabulary for main.off_target_reason — never an admission
        # test. See the Home Theater entry and OFF_TARGET_TERMS. Broader than
        # Home Theater's because this persona's vocabulary is broader: a
        # cleaning routine, a nursery reveal and a Christmas decor haul are all
        # the same customer. Measured over the 85 rows live on 2026-08-21, no
        # Lifestyle row was flagged off-target at all (the highest scored 0.04),
        # so these terms currently rescue nothing — they are here so the
        # gaming/tech vocabulary can never start eating this niche either.
        "on_target_terms": [
            "home decor", "decor", "interior", "styling", "organiz",
            "declutter", "cleaning", "clean with me", "diy", "makeover",
            "renovation", "house tour", "home tour", "apartment tour",
            "furniture", "homeware", "cozy", "cosy", "minimalist", "farmhouse",
            "kitchen", "bedroom", "living room", "nursery", "seasonal",
            "christmas", "haul", "recipe", "cooking", "baking", "mom",
            "motherhood", "family", "homemaking", "self care", "morning routine",
            "home office", "small home", "tiny home", "house plan",
        ],
        # GEMINI RESCUE CRITERIA. Read only when main's title-based
        # off_target_reason has FLAGGED a candidate, and used only to overturn
        # that flag — never to drop a candidate the keyword gate let through.
        # See gemini_verify.GeminiVerifier.judge for why that asymmetry is the
        # whole safety argument.
        #
        # Two rules for editing these:
        #   1. A VIDEO criterion must be answerable from ~25 seconds of footage
        #      alone. "Does the creator own their home" is not; "is a person
        #      presenting to camera" is.
        #   2. Editing either list invalidates every cached verdict for this
        #      niche automatically (the criteria are hashed into the cache key),
        #      so retuning costs requests, never correctness.
        # Keep to 2-4 entries per list: each one is a separate judgement the
        # model has to evidence, and a long list dilutes all of them.
        "text_criteria": [
            {"name": "home and living focus",
             "test": "Across these titles and descriptions, is the channel's "
                     "recurring subject the home itself — decorating, styling, "
                     "organising, cleaning, furnishing, renovating, house tours, "
                     "homemaking — rather than fashion, beauty, travel, fitness "
                     "or general vlogging that merely happens indoors?"},
            {"name": "shows real spaces",
             "test": "Does the channel show real, specific rooms and furniture it "
                     "has access to, rather than aggregating inspiration images "
                     "or reposting other people's interiors?"},
        ],
        # LOOSENED 2026-08-21. See the Home Theater entry for the measurement
        # that prompted it. The criterion dropped here was the strictest of all
        # six: "would a sofa brand recognise its own product category" ruled out
        # kitchen, bedroom, organising and cooking content from creators whose
        # audience is exactly the target. A lived-in home on screen is the bar.
        "video_criteria": [
            {"name": "a real home, and life in it",
             "test": "Does this clip show a real home interior or home life: "
                     "decorating, styling, organising, cleaning, cooking, hosting, "
                     "a room or house tour, a renovation, or everyday routines at "
                     "home? A lived-in room on screen counts even when furniture "
                     "is not what is being talked about."},
            {"name": "a real creator, not a repost",
             "test": "Is there a real person on camera, OR a voice narrating "
                     "footage they appear to have shot themselves? Answer no only "
                     "if this looks like stock footage, a slideshow of stills, or "
                     "reposted material with no creator present."},
            {"name": "an independent creator, not a brand",
             # REQUIRED: a veto, not a scored criterion. The ratio route above is
             # meant to loosen how much CONTENT relevance is demanded, and a
             # manufacturer or publisher is not two-thirds eligible. Measured:
             # ADAM Audio was correctly caught here and then re-admitted at 2/3
             # before this flag existed.
             "required": True,
             "test": "Is this an individual creator's own channel, rather than a "
                     "company, retailer, publisher, manufacturer, studio or TV "
                     "brand posting produced marketing content? Signs of a brand: "
                     "polished agency-style production with no identifiable host, "
                     "a presenter speaking on behalf of a company, product B-roll "
                     "with voiceover and no personality, or a logo bug throughout. "
                     "A single person filming in their own home or workshop is an "
                     "independent creator even when the production is good."},
        ],
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
            # REWRITTEN 2026-08-21. The previous wording LED WITH "fashion and
            # lifestyle vlogs, travel", which is exactly the generic-lifestyle
            # creator the brief rules out: "do NOT treat a generic lifestyle
            # influencer as a good match just because they post occasional
            # lifestyle content." It is why this table filled with travel
            # vloggers, beauty creators and fitness channels — Travel For
            # Phoebe, Traveling with Kristin, Trini Surfer, LeanBeefPatty,
            # Emily Canham — none of whose audiences are shopping for a sofa.
            #
            # Now home-first. Re-probed at limit=1 (0.01 credits) per the same
            # rule as Home Theater. Measured totals:
            #   current, fashion/travel first ................ 2,120
            #   home-first, fashion/travel dropped ........... 1,501  <- this
            #   + "homemaking" phrasing ...................... 1,492
            #   + "family home life" ......................... 1,018
            #   + "furniture and homeware, cozy living" ......   997
            #   + "seasonal decorating, decor hauls" .........   782
            # Smaller and on-persona beats larger and generic; the relevance
            # gate cannot rescue a pool that was never the right pool.
            #
            # CONFIRMED ON CHARACTER, not just on size — the check Home Theater's
            # entry explains at length. Top of the ranking under this wording:
            # Lexi DIY, HerDIYHome, Cozy DIY Home, Carissa Cleans It All,
            # Alexandra Gater, Canterbury Cottage, Minimal Ease, Nora G's Nook,
            # Karin Bohn. The narrower "furniture and homeware, cozy living"
            # variant (994) returned more undifferentiated vloggers at the top,
            # so it was rejected despite reading like the tighter query.
            "ai_search": "home decor and interior styling, house tours, home organization and cleaning, DIY home makeovers",
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
# Value lives in config.py — it is env-tunable and config.py is the only
# module that reads environment variables. See there for the measurement
# that set it and for why lowering it loosens no quality gate.
DISCOVERY_SUBSCRIBER_FLOOR_RATIO = CONFIG_DISCOVERY_SUBSCRIBER_FLOOR_RATIO


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
# ---------------------------------------------------------------------------
# RELEVANCE: what an off-target channel looks like (2026-08-21)
# ---------------------------------------------------------------------------
#
# The brief: the tables were filling with gaming and generic-tech creators, and
# "a YouTuber who reviews iPhones, laptops, gaming PCs and gadgets" is a bad
# match however good their numbers are. Measured over the 147 rows then live,
# 29 of the 64 Home Theater rows (45%) were off-target by this definition.
#
# WHY THIS IS A NEGATIVE VOCABULARY AND NOT A POSITIVE ONE. A positive
# must-match-a-term gate was built and measured on 2026-08-15 and REJECTED (the
# reasoning is preserved above EXCLUDED_TOPIC_TERMS): it discarded "Jasper Tran
# - House Design Ideas", a real prospect, on a positive score of 0/50, while
# missing an off-niche logging channel whose woodworking titles carried
# "furniture" and "interior". Requiring a channel to PROVE it belongs is the
# thing that does not work, because plenty of genuine prospects use no
# vocabulary a list can anticipate ("This Small House Will Make You Fall in
# Love"). So nothing here is ever required. A channel is dropped only on
# positive evidence that it is something ELSE, which keeps the pipeline's
# standing rule that absent data never disqualifies.
#
# ON_TARGET_TERMS (per niche, in the NICHES dict above) exist ONLY to RESCUE a
# channel these terms flagged — never to admit one. That asymmetry is what lets
# the gate keep "OCM Reviews" (DACs, IEMs, Atmos soundbars: off 0.06 / on 0.60)
# while dropping "DragsterTV" (Rainbow Six and Forza money glitches: off 0.04 /
# on 0.00) at almost the same off-target score. One signal cannot separate those
# two; the on-target rescue is what does.
#
# TERM SELECTION IS PRECISION-FIRST. Deliberately ABSENT, because each is
# legitimately on-niche for Home Theater: speaker, soundbar, receiver,
# projector, TV, 4K, HDR, atmos, amplifier, headphone, subwoofer, and the bare
# word "gaming" — a man cave with a gaming setup is a prospect, a Fortnite
# gameplay channel is not, and GAME TITLES are what tell those apart. That is
# the same argument the sim_racing entry above already makes.
OFF_TARGET_TERMS = {
    # Game titles and console platforms, never the bare word "gaming".
    "gaming": [
        "fortnite", "roblox", "minecraft", "call of duty", "warzone", "valorant",
        "overwatch", "apex legends", "league of legends", "counter-strike", "cs2",
        "gta v", "gta 6", "elden ring", "zelda", "pokemon", "rainbow six", "forza",
        "modern warfare", "battle pass", "playstation", "ps5", "ps4", "xbox",
        "nintendo", "dualsense", "gameplay", "speedrun", "let's play", "lets play",
        "steam deck", "esports", "fps montage", "game review", "gamer",
    ],
    # Phones, laptops and PC hardware — named in the brief as must-exclude.
    # "pc build" and friends rather than a bare "pc", because an HTPC in a media
    # rack is on-niche.
    "phones_and_pcs": [
        "iphone", "android phone", "galaxy s2", "galaxy z", "pixel 9", "pixel 10",
        "pixel 11", "smartphone", "macbook", "chromebook", "ipad", "laptop review",
        "gaming pc", "gaming laptop", "rtx 40", "rtx 50", "radeon", "ryzen",
        "intel core", "motherboard", "overclock", "gpu", "benchmark", "ssd review",
        "5g phone", "phone case", "foldable phone", "pc build", "pcbuild",
        "pc builds", "diy pc", "pc case", "pc parts", "custom pc", "pcgaming",
        "gaming monitor", "gaming handheld", "gaming chair", "gaming keyboard",
        "mechanical keyboard", "lian li", "thermaltake", "steamos", "water cooling",
        "liquid cooling", "rgb build", "mini pc", "geforce", "nvidia",
        "cable management",
    ],
    # The general-consumer-electronics channel: reviews whatever arrives in the
    # post. Audio gear is deliberately absent from this list.
    "generic_gadgets": [
        "robot vacuum", "robotic vacuum", "vacuum mop", "dehumidifier",
        "air purifier", "treadmill", "power station", "smartwatch",
        "fitness tracker", "earbuds", "electric scooter", "3d printer",
        "drone review", "action camera", "osmo pocket", "insta360", "dash cam",
        "massage gun",
    ],
    # Toys, construction-brick and kids' doll/roleplay content. ADDED 2026-08-22
    # from reviewer feedback: Victor rejected "Bricksie" (a LEGO City channel)
    # and "Baby Doll Stories" for Home Theater, and neither was visible to this
    # gate at all — both scored 0.00 off-target and sailed through.
    #
    # Baby Doll Stories is the more instructive one. Its titles say "Room
    # Makeover" and "DIY Cardboard ... Doll Room", so it scores ON-TARGET for
    # Lifestyle Sofa ("makeover", "diy"). A kids' doll channel was reading as a
    # home-decor prospect. That is why this category is global rather than
    # Home-Theater-only.
    #
    # SUBSTRING SAFETY IS THE WHOLE DIFFICULTY HERE — matching is plain
    # `term in title.lower()`, so the obvious words are traps:
    #   "brick" matches "brick wall" / "exposed brick fireplace"  <- real decor
    #   "doll"  matches "dollar store decor"                      <- real decor
    #   "toy"   matches "toy storage ideas" and "Toyota"          <- real, and a car
    #   "figure" matches "figure out"
    # So every entry below is either a brand ("lego", "barbie", "funko") or a
    # multi-word phrase. Do not "simplify" these to their root words; there is a
    # test that fails if you do.
    "toys_and_kids": [
        "lego", "minifigure", "minifig", "brickheadz", "bricklink",
        "barbie", "baby doll", "dollhouse", "doll house", "doll room",
        "doll story", "doll makeover", "squishmallow", "squishy", "slime",
        "play doh", "play-doh", "playdough", "playset",
        # "toy story" was here and was REMOVED 2026-08-22: it matched
        # "Toy Story Gaming Laptop! MSI Cyborg 15 Special Edition" on Paul
        # Antill, a channel the reviewer APPROVED. A film brand shows up on
        # branded merchandise, so it is not evidence of kids' content.
        "toy review", "toy unboxing", "toy haul", "kids toy",
        "surprise egg", "blind bag", "action figure", "funko",
        "poor vs rich", "rich vs poor", "gacha", "kids cartoon",
        "nursery rhyme",
    ],
    # Story-recap content (manhwa / manhua / webtoon / anime). ADDED 2026-08-22
    # on reviewer instruction after "1221 Manhwa Recap" reached the Home Theater
    # table from the "movie review and reaction" keyword.
    #
    # The keyword itself is KEPT. Removing it would cost real volume — movie and
    # reaction creators are a plausible home-entertainment audience — so the
    # content type is excluded instead of the query that found it.
    #
    # Measured: kills 0 of 21 approved and 0 of 31 rejected in Home Theater, and
    # 0/37 // 0/53 in Lifestyle. No historical evidence either way, because this
    # content only started appearing once Home Theater moved to the free
    # search.list corpus. Shipped on the instruction plus the zero-harm result,
    # not on a measured benefit.
    "story_recap": [
        "manhwa", "manhua", "webtoon", "anime recap", "manga recap",
        "donghua", "light novel", "novel recap",
    ],
    # AV-SPECIALIST vocabulary, and this one is the surprise. MEASURED 2026-08-22
    # over 21 approved / 31 rejected Home Theater rows:
    #
    #   as an EXCLUSION: catches 5 rejected, kills 0 approved
    #   as RESCUE vocab: rescues 6 REJECTED and 0 APPROVED
    #
    # "speakers" appears in 0 of 21 approved channels and 8 of 31 rejected;
    # "audio" in 2 of 21 vs 10 of 31. The reviewer rejects the dedicated hi-fi
    # reviewers — Zero Fidelity, New Record Day, Forever Analog, 5.1 Test &
    # Clips — and the manufacturer accounts, ADAM Audio and Dolby.
    #
    # So this vocabulary was on the WRONG SIDE of the gate: it sat in Home
    # Theater's on_target_terms, where its only measurable effect was to rescue
    # channels the reviewer had rejected. It is an exclusion now, and it is
    # removed from on_target_terms below — both halves are required, because
    # a term on both lists scores off == on and the gate needs off > on.
    #
    # NOT applied to Lifestyle: untested there, and unmeasured strictness is
    # what this whole exercise has been undoing.
    "av_specialist": [
        # "speaker" is the single strongest signal in the labelled set: it
        # appears in 0 of 21 approved Home Theater channels and 8 of 31
        # rejected. Bare rather than a phrase, because unlike "brick" or "doll"
        # it has no common collision with real home-content vocabulary.
        "speaker", "subwoofer", "audiophile", "hi-fi", "hifi", " dac", "iem",
        "amplifier", "turntable", "bookshelf speaker", "klipsch", "denon",
        "marantz", "phono", "loudspeaker",
    ],
    # Property showcase and travel, for LIFESTYLE only. Measured there:
    # property_showcase catches 2 rejected / kills 0; travel_vlog 2 / 0. Small
    # but free. They match the rejected set's character — Homeworthy England,
    # Homeworthy New York, Escape To The Country, Inside Japan Living — which
    # is aspirational property media rather than a creator selling to an
    # audience.
    #
    # `realestate_listing` was tested alongside these and REJECTED: +2 rejected
    # but -1 approved. A net of +1 is not worth a lost prospect when the brief
    # is explicitly "still want many output, not super strict".
    "property_showcase": [
        "inside the", "mansion", "estate tour", "property tour",
        "million dollar", "luxury home", "home tour of",
    ],
    "travel_vlog": [
        "travel vlog", "country in", "visiting", "trip to", "backpacking", "expat",
    ],
    # ADDED 2026-08-22 from reviewer feedback on the first 90-day sweep: "some
    # channels are not suitable for Home Theater" and "I only got 3 new
    # channels" out of 29 pushed. The sweep fixed volume and exposed precision.
    #
    # Each set was scored against BOTH the 142 historical verdicts AND the 67
    # unreviewed rows the sweep produced. All four kill ZERO approved channels
    # in either niche and together catch 17 of the 67:
    #
    #   sports_commentary   0 approved / 9 caught   DNVR Sports, Nightcap,
    #                                               Sky Sports Cricket, The Pivot
    #   automotive          0 approved / 3 caught   CAR TV, Moto Feelz
    #   movie_review_farm   0 approved / 3 caught   Reel Review HQ, Media Knights
    #   kids_craft          0 approved / 2 caught   123 GO!, 123 GO! GOLD
    #
    # kids_craft is the gap toys_and_kids left: 123 GO! is a 12.7M-subscriber
    # kids channel whose titles are "hacks" and "pranks" rather than any toy
    # brand, so no amount of Lego vocabulary would have caught it.
    "sports_commentary": [
        "podcast", "nfl", "nba", "mlb", "nhl", "premier league", "arsenal",
        "cricket", "wrestling", "wwe", "aew", "transfer news", "match preview",
        "game recap", "fantasy football", "draft pick", "free agency", "playoff",
    ],
    "automotive": [
        "car review", "truck review", "suv", "sedan", "test drive", "horsepower",
        "mustang", "corvette", "motorcycle", "moto ", "dealership",
        "engine swap", "car tv",
    ],
    "movie_review_farm": [
        "movie review", "film review", "trailer reaction", "first time watching",
        "explained ending", "recap and review", "season finale",
        "episode review", "box office",
    ],
    "kids_craft": [
        "hacks you", "genius hacks", "crafts for", "funny pranks", "diy hacks",
        "life hacks", "123 go", "tricks and hacks", "weird ways",
    ],
    "ai_and_crypto": [
        "crypto", "bitcoin", "ethereum", "nft", "altcoin", "web3", "blockchain",
        "ai tool", "ai platform", "ai automation", "ai review", "chatgpt",
        "midjourney", "ai agent", "saas",
    ],
}

# What a creator SAYS their channel is. High precision, because these are the
# creator's own claims about their own channel ("Gamer / lover of pop culture",
# "hands-on guide to DIY PC", "Its all about technology and gadgets").
#
# Used only by the PERSONA rule, which additionally requires the video titles to
# corroborate. A bio alone never drops a channel: bios are written once and go
# stale, four tracked channels have no usable bio at all, and "High-quality
# Tech, Unboxing, Reviews" is OCM Reviews — a genuine hi-fi channel.
BIO_OFF_SIGNALS = [
    "gamer", "gaming channel", "playing games", "video game", "let's play",
    "lets play", "twitch", "speedrun", "money glitches", "fortnite", "roblox",
    "minecraft", "playstation", "xbox", "nintendo", "esports",
    "about technology", "all about tech", "tech world", "tech reviews",
    "technology and gadgets", "gadget reviews", "latest technology",
    "consumer electronics", "smartphone", "unboxing and product reviews",
    "crypto", "nft", "blockchain", "ai tools",
    "diy pc", "pc building", "pc builds", "pc component", "gaming pc",
]

OFF_TARGET_KEYWORDS = sorted(
    {term for terms in OFF_TARGET_TERMS.values() for term in terms}
)

# The subset of the above sent to the VENDOR as keywords_not_in_description, so
# a gaming or generic-tech creator is never returned and never billed the 0.01.
#
# DELIBERATELY A SMALL SUBSET, not OFF_TARGET_KEYWORDS. Two reasons. First, this
# filter reads the BIO, and a bio is the weak signal — the local gate reads 50
# video titles and is what actually does the work (it rejects 46% of Home
# Theater's current rows; these bio terms remove only ~18 creators from the
# pool). Second, every term here silently shrinks the discovery pool, and a
# false negation is unrecoverable: the creator is never shown to us at all. So
# only high-precision, self-descriptive phrases are here. "nvidia" is absent on
# purpose — an NVIDIA Shield is a streaming box a home-theater creator may well
# mention — and so are "speaker", "projector" and every other on-niche word.
#
# Pool cost measured at limit=1 on 2026-08-21, per the re-probe rule above:
#   Home Theater .... 250 -> 232 (7% narrower)
#   Lifestyle Sofa .. 1,501 -> 1,498 (0%)
GAMING_AND_TECH_BIO_NEGATIONS = [
    "fortnite", "roblox", "minecraft", "call of duty", "valorant", "video game",
    "gamer", "twitch", "esports", "playstation", "xbox", "nintendo",
    "tech reviews", "gadget reviews", "consumer electronics", "smartphone",
    "pc building", "gaming pc",
]


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
        # Off-brand topics AND the gaming/generic-tech negations, merged and
        # deduped. Both are bio-level filters that save a 0.01 discovery credit
        # each; neither is the relevance decision, which main.off_target_reason
        # makes off the video titles.
        filters["keywords_not_in_description"] = sorted(
            set(EXCLUDED_TOPIC_KEYWORDS) | set(GAMING_AND_TECH_BIO_NEGATIONS)
        )

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
